from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file, make_response
from flask_compress import Compress
from functools import wraps
import contextlib, copy, json, os, uuid, secrets, time, base64, threading
import urllib.request as _urllib
from datetime import datetime, timedelta
from collections import defaultdict
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from PIL import Image
import io
import psycopg2
from psycopg2 import pool as pgpool
from psycopg2.extras import RealDictCursor
import cloudinary
import cloudinary.uploader

app = Flask(__name__, template_folder='templates', static_folder='static')
Compress(app)

_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise RuntimeError("SECRET_KEY mühit dəyişəni təyin edilməlidir")
app.secret_key = _secret_key
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB upload limiti
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_DEBUG', '') not in ('1', 'true')

# ── LOGIN RATE LIMITING ──
_login_attempts: dict = defaultdict(list)
_LOGIN_MAX = 5
_LOGIN_WINDOW = 300  # 5 dəqiqə

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    return len(_login_attempts[ip]) >= _LOGIN_MAX

def _record_failed_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())

def _clear_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)

# ── FORGOT-PASSWORD RATE LIMITING (ayrı bucketdə) ──
_forgot_attempts: dict = defaultdict(list)
_FORGOT_MAX = 3
_FORGOT_WINDOW = 300  # 5 dəqiqə

# ── MENYU DATA CACHE (server-side, public sorğular üçün) ──
_data_cache: dict = {}   # {username: (data_dict, timestamp)}
_DATA_CACHE_TTL = 300    # saniyə (5 dəqiqə)

def _get_cached_data(username: str):
    entry = _data_cache.get(username)
    if entry and time.time() - entry[1] < _DATA_CACHE_TTL:
        return entry[0]
    return None

def _set_cached_data(username: str, data: dict) -> None:
    _data_cache[username] = (data, time.time())

def _invalidate_cache(username: str) -> None:
    _data_cache.pop(username, None)

# ── CLOUDFLARE WORKER PUSH ──
_CF_WORKER_URL    = 'https://qr-menu-worker.qr-menu-az.workers.dev'
_CF_INTERNAL_SECRET = os.environ.get('CF_INTERNAL_SECRET', '')

def _push_to_cf_worker(username: str, html: str) -> None:
    if not _CF_INTERNAL_SECRET:
        return
    try:
        api_url = f'{_CF_WORKER_URL}/internal/menu/{username}'
        req = _urllib.Request(api_url,
            data=html.encode('utf-8'),
            headers={
                'X-Internal-Secret': _CF_INTERNAL_SECRET,
                'Content-Type': 'text/html; charset=utf-8',
                'User-Agent': 'qr-menu-flask/1.0',
            },
            method='PUT')
        with _urllib.urlopen(req, timeout=10):
            pass
    except Exception:
        pass

def _push_static_menu_async(username: str) -> None:
    try:
        # Cache-dən yox, birbaşa DB-dən oxu (save sonrası cache boş olur)
        data = None
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT data FROM user_data WHERE username = %s", (username,))
                row = cur.fetchone()
                if row:
                    data = row['data']
        except Exception:
            pass
        if data is None:
            return
        pub = {k: data[k] for k in ('cafe', 'categories', 'items', 'theme') if k in data}
        def _strip_b64(obj):
            if isinstance(obj, dict):
                return {k: ('' if isinstance(v, str) and v.startswith('data:') else _strip_b64(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_strip_b64(i) for i in obj]
            return obj
        pub = _strip_b64(pub)
        with app.app_context():
            html = render_template('menu.html',
                menu_user=username,
                embedded_data=json.dumps(pub, ensure_ascii=False))
        # Relative static path-ları Railway absolute URL-ə çevir
        _railway = 'https://qr-menu-az.up.railway.app'
        html = html.replace('href="/static/', f'href="{_railway}/static/')
        html = html.replace('src="/static/', f'src="{_railway}/static/')
        _push_to_cf_worker(username, html)
    except Exception:
        pass

# ── USERS CACHE ──
_users_cache: dict = {'data': None, 'ts': 0.0}
_USERS_CACHE_TTL = 600  # saniyə (10 dəqiqə)

def _get_cached_users():
    if _users_cache['data'] is not None and time.time() - _users_cache['ts'] < _USERS_CACHE_TTL:
        return _users_cache['data']
    return None

def _set_cached_users(users: dict) -> None:
    _users_cache['data'] = users
    _users_cache['ts'] = time.time()

def _invalidate_users_cache() -> None:
    _users_cache['data'] = None

def _is_forgot_limited(ip: str) -> bool:
    now = time.time()
    _forgot_attempts[ip] = [t for t in _forgot_attempts[ip] if now - t < _FORGOT_WINDOW]
    return len(_forgot_attempts[ip]) >= _FORGOT_MAX

def _record_forgot_attempt(ip: str) -> None:
    _forgot_attempts[ip].append(time.time())

# ── CLOUDINARY KONFİQURASİYASI ──
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

# ── EMAIL KONFİQURASİYASI ──
_mail_user = os.environ.get('MAIL_USERNAME')
_mail_pass = os.environ.get('MAIL_PASSWORD')
if not _mail_user or not _mail_pass:
    import warnings
    warnings.warn(
        "MAIL_USERNAME və ya MAIL_PASSWORD mühit dəyişəni təyin edilməyib.",
        RuntimeWarning
    )

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = _mail_user
app.config['MAIL_PASSWORD'] = _mail_pass
app.config['MAIL_DEFAULT_SENDER'] = ('QR Menu', _mail_user)

APP_BASE_URL = os.environ.get('APP_BASE_URL', 'https://ucbucaqq-production.up.railway.app')

mail = Mail(app)

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# ────────────────────────────────────────────────────────────────
# CONNECTION POOL
# ────────────────────────────────────────────────────────────────

_db_pool = None

def _get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pgpool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=os.environ.get('DATABASE_URL'),
            cursor_factory=RealDictCursor
        )
    return _db_pool

@contextlib.contextmanager
def db_conn():
    """Pool-dan connection götür, commit et, qaytar. Köhnəlmiş connection-u avtomatik yenilər."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Bağlantının canlı olduğunu yoxla, köhnəlmişsə yenilə
        try:
            conn.cursor().execute("SELECT 1")
        except Exception:
            pool.putconn(conn, close=True)
            conn = pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass

# ────────────────────────────────────────────────────────────────
# DEFAULT MENYU STRUKTURU
# ────────────────────────────────────────────────────────────────

DEFAULT_MENU_DATA = {
    "cafe": {
        "nameAz": "Restoran", "nameEn": "Restaurant",
        "addrAz": "Bakı", "addrEn": "Baku",
        "phone": "", "icon": "☕",
        "whatsapp": "", "instagram": "", "tiktok": "", "maps": ""
    },
    "categories": [
        {"id": "coffee",  "labelAz": "Qəhvə",  "labelEn": "Coffee",   "bg": "#FFF3E0"},
        {"id": "tea",     "labelAz": "Çay",     "labelEn": "Tea",      "bg": "#E8F5E9"},
        {"id": "food",    "labelAz": "Yemək",   "labelEn": "Food",     "bg": "#FFF8E1"},
        {"id": "dessert", "labelAz": "Desert",  "labelEn": "Desserts", "bg": "#FCE4EC"}
    ],
    "items": [],
    "theme": {
        "id": "classic",
        "vars": {
            "accent": "#E8622A", "bg": "#FDF8F3", "card": "#FFFFFF",
            "text": "#1A1210", "muted": "#8B7355",
            "border": "rgba(180,140,100,0.18)",
            "header": "#E8622A", "headerText": "#ffffff"
        }
    },
    "stats": {"clicks": {}, "opens": {"total": 0, "dates": {}}, "cats": {}}
}

# ────────────────────────────────────────────────────────────────
# DB BAŞLANĞICI
# ────────────────────────────────────────────────────────────────

def init_db():
    with db_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username             TEXT PRIMARY KEY,
                password             TEXT NOT NULL,
                role                 TEXT NOT NULL DEFAULT 'manager',
                email                TEXT DEFAULT '',
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_data (
                username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
                data     JSONB NOT NULL DEFAULT '{}'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reset_tokens (
                token    TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires  TIMESTAMPTZ NOT NULL,
                used     BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        # Köhnə sxemdə expires TEXT ola bilər — migration
        cur.execute("""
            ALTER TABLE reset_tokens
            ALTER COLUMN expires TYPE TIMESTAMPTZ
            USING expires::timestamptz
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id       SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                action   TEXT NOT NULL,
                detail   JSONB DEFAULT '{}',
                ip       TEXT DEFAULT '',
                ts       TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC)")
        # Vaxtı keçmiş və ya istifadə edilmiş tokenləri təmizlə
        cur.execute("DELETE FROM reset_tokens WHERE used = TRUE OR expires::timestamptz < NOW()")

        cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            _admin_pw = os.environ.get('ADMIN_PASSWORD')
            _must_change = False
            if not _admin_pw:
                _admin_pw = secrets.token_urlsafe(14)
                _must_change = True
                print(f"[WARN] ADMIN_PASSWORD təyin edilməyib. İlk giriş şifrəsi: {_admin_pw}", flush=True)
            cur.execute(
                "INSERT INTO users (username, password, role, email, must_change_password) VALUES (%s,%s,%s,%s,%s)",
                ('admin', generate_password_hash(_admin_pw), 'superadmin', '', _must_change)
            )
            cur.execute(
                "INSERT INTO user_data (username, data) VALUES (%s,%s)",
                ('admin', json.dumps(DEFAULT_MENU_DATA))
            )

# ────────────────────────────────────────────────────────────────
# İSTİFADƏÇİ ƏMƏLIYYATLARI
# ────────────────────────────────────────────────────────────────

def load_users():
    cached = _get_cached_users()
    if cached is not None:
        return cached
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT username, password, role, email, must_change_password FROM users")
        rows = cur.fetchall()
    users = {r['username']: {
        'password': r['password'],
        'role': r['role'],
        'email': r['email'] or '',
        'must_change_password': r['must_change_password']
    } for r in rows}
    _set_cached_users(users)
    return users

def save_user(username, password_hash, role, email=''):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (username, password, role, email) VALUES (%s,%s,%s,%s)
               ON CONFLICT (username) DO UPDATE
               SET password=EXCLUDED.password, role=EXCLUDED.role, email=EXCLUDED.email""",
            (username, password_hash, role, email)
        )
    _invalidate_users_cache()

def delete_user(username):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (username,))
    _invalidate_users_cache()

# ────────────────────────────────────────────────────────────────
# MENYU DATA — race-condition-free
# ────────────────────────────────────────────────────────────────

def load_user_data(username):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT data FROM user_data WHERE username = %s", (username,))
        row = cur.fetchone()
        if row:
            return row['data']
        data = copy.deepcopy(DEFAULT_MENU_DATA)
        cur.execute(
            "INSERT INTO user_data (username, data) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (username, json.dumps(data, ensure_ascii=False))
        )
        return data

def _upsert_user_data(cur, username, data):
    cur.execute(
        """INSERT INTO user_data (username, data) VALUES (%s,%s)
           ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data""",
        (username, json.dumps(data, ensure_ascii=False))
    )

def log_action(username, action, detail=None, ip=''):
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO audit_log (username, action, detail, ip) VALUES (%s,%s,%s::jsonb,%s)",
                (username, action, json.dumps(detail or {}, ensure_ascii=False), ip or '')
            )
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────
# KÖMƏKÇİLƏR
# ────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def current_user():
    return session.get('user')

def resize_image(file_obj, max_size=(1200, 1200), quality=82):
    img = Image.open(file_obj)
    try:
        from PIL.ExifTags import TAGS
        exif = img._getexif()
        if exif:
            for tag, val in exif.items():
                if TAGS.get(tag) == 'Orientation':
                    rotations = {3: 180, 6: 270, 8: 90}
                    if val in rotations:
                        img = img.rotate(rotations[val], expand=True)
                    break
    except Exception:
        pass

    img.thumbnail(max_size, Image.LANCZOS)

    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        buf.seek(0)
        return buf, 'png'

    if img.mode != 'RGB':
        img = img.convert('RGB')

    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=quality, optimize=True)
    buf.seek(0)
    return buf, 'jpg'

def upload_to_cloudinary(file_obj, folder='ucbucaq', public_id=None, max_size=(1200, 1200)):
    try:
        buf, fmt = resize_image(file_obj, max_size=max_size)
    except Exception:
        file_obj.seek(0)
        buf = file_obj
        fmt = 'jpg'

    result = cloudinary.uploader.upload(
        buf,
        folder=folder,
        public_id=public_id,
        overwrite=True,
        resource_type='image'
    )
    return result['secure_url']

# ────────────────────────────────────────────────────────────────
# AUTH DEKORATORlari
# ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Giriş tələb olunur'}), 401
        return f(*args, **kwargs)
    return decorated

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Giriş tələb olunur'}), 401
        if session.get('role') != 'superadmin':
            return jsonify({'error': 'Superadmin səlahiyyəti lazımdır'}), 403
        return f(*args, **kwargs)
    return decorated

# ────────────────────────────────────────────────────────────────
# SƏHİFƏLƏR
# ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('menu'))

@app.route('/menu')
def menu():
    return render_template('menu.html', menu_user='')

@app.route('/menu/<username>')
def menu_user(username):
    users = load_users()
    embedded = None
    if username in users:
        cached = _get_cached_data(username)
        if cached is None:
            cached = load_user_data(username)
            _set_cached_data(username, cached)
        pub = {k: cached[k] for k in ('cafe', 'categories', 'items', 'theme') if k in cached}
        # Strip base64 images from embedded data — they inflate HTML to MBs.
        # Browser will show emoji icon instead; Cloudinary URLs are kept as-is.
        def _strip_b64(obj):
            if isinstance(obj, dict):
                return {k: ('' if isinstance(v, str) and v.startswith('data:') else _strip_b64(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_strip_b64(i) for i in obj]
            return obj
        pub = _strip_b64(pub)
        embedded = json.dumps(pub, ensure_ascii=False)
    return render_template('menu.html', menu_user=username, embedded_data=embedded)

@app.route('/admin')
def admin():
    return render_template('admin.html')

# ────────────────────────────────────────────────────────────────
# AUTH API
# ────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if _is_rate_limited(ip):
        return jsonify({'ok': False, 'error': 'Çox sayda uğursuz cəhd. 5 dəqiqə gözləyin.'}), 429

    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    users = load_users()
    if username in users and check_password_hash(users[username]['password'], password):
        role = users[username].get('role', 'manager')
        must_change = users[username].get('must_change_password', False)
        session.permanent = True
        session['user'] = username
        session['role'] = role
        _clear_attempts(ip)
        log_action(username, 'login', {}, ip)
        return jsonify({'ok': True, 'username': username, 'role': role, 'must_change_password': must_change})
    _record_failed_attempt(ip)
    log_action(username or '?', 'login_failed', {}, ip)
    return jsonify({'ok': False, 'error': 'İstifadəçi adı və ya şifrə yanlışdır'}), 401

@app.route('/api/logout', methods=['POST'])
def api_logout():
    user = current_user()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if user:
        log_action(user, 'logout', {}, ip)
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me')
def api_me():
    if 'user' in session:
        return jsonify({'ok': True, 'username': session['user'], 'role': session.get('role', 'manager')})
    return jsonify({'ok': False}), 401

# ────────────────────────────────────────────────────────────────
# DATA API
# ────────────────────────────────────────────────────────────────

@app.route('/api/data')
def api_get_data():
    requested_user = request.args.get('user')
    logged_in = current_user()
    username = requested_user or logged_in or 'admin'
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404

    is_public = bool(requested_user and requested_user != logged_in)

    if is_public:
        # Server cache-dən oxu — DB sorğusundan qaç
        cached = _get_cached_data(username)
        if cached is None:
            cached = load_user_data(username)
            _set_cached_data(username, cached)
        data = {k: cached[k] for k in ('cafe', 'categories', 'items', 'theme') if k in cached}
        resp = make_response(jsonify(data))
        resp.headers['Cache-Control'] = 'public, max-age=30'
        return resp

    db = load_user_data(username)
    resp = make_response(jsonify(db))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/api/data', methods=['PUT'])
@login_required
def api_save_data():
    username = current_user()
    incoming = request.json or {}
    allowed_keys = {'cafe', 'categories', 'items', 'theme'}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT data FROM user_data WHERE username = %s FOR UPDATE", (username,))
        row = cur.fetchone()
        db = row['data'] if row else copy.deepcopy(DEFAULT_MENU_DATA)
        changed = [k for k in incoming if k in allowed_keys]
        for key in changed:
            db[key] = incoming[key]
        _upsert_user_data(cur, username, db)
    _invalidate_cache(username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(username, 'save_data', {'keys': changed}, ip)
    # Yenilənmiş menyunu GitHub Pages-ə arxa fonda push et
    threading.Thread(target=_push_static_menu_async, args=(username,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/sync', methods=['POST'])
@login_required
def api_sync_menu():
    username = current_user()
    threading.Thread(target=_push_static_menu_async, args=(username,), daemon=True).start()
    return jsonify({'ok': True})

# ────────────────────────────────────────────────────────────────
# ŞƏKİL YÜKLƏMƏ — Cloudinary
# ────────────────────────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'Fayl tapılmadı'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Fayl seçilmədi'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Yalnız PNG, JPG, GIF, WEBP faylları qəbul edilir'}), 400

    try:
        username = current_user()
        public_id = f"ucbucaq/{username}/items/{uuid.uuid4()}"
        url = upload_to_cloudinary(file, public_id=public_id)
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload/logo', methods=['POST'])
@login_required
def api_upload_logo():
    if 'file' not in request.files:
        return jsonify({'error': 'Fayl tapılmadı'}), 400
    file = request.files['file']
    if not allowed_file(file.filename):
        return jsonify({'error': 'Yalnız şəkil faylları'}), 400

    username = current_user()
    try:
        public_id = f"ucbucaq/{username}/logo"
        url = upload_to_cloudinary(file, public_id=public_id, max_size=(400, 400))
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT data FROM user_data WHERE username = %s FOR UPDATE", (username,))
            row = cur.fetchone()
            db = row['data'] if row else copy.deepcopy(DEFAULT_MENU_DATA)
            db['cafe']['logo'] = url
            _upsert_user_data(cur, username, db)
        _invalidate_cache(username)
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        log_action(username, 'upload_logo', {'url': url}, ip)
        threading.Thread(target=_push_static_menu_async, args=(username,), daemon=True).start()
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ────────────────────────────────────────────────────────────────
# İSTİFADƏÇİ İDARƏETMƏSİ
# ────────────────────────────────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@login_required
def api_get_users():
    users = load_users()
    current_role = session.get('role', 'manager')
    result = {}
    for k, v in users.items():
        if v.get('role') == 'superadmin' and current_role != 'superadmin':
            continue
        result[k] = {'role': v.get('role', 'manager'), 'email': v.get('email', '')}
    return jsonify(result)

@app.route('/api/users', methods=['POST'])
@superadmin_required
def api_add_user():
    data = request.json or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')
    role = data.get('role', 'manager')
    email = data.get('email', '').strip()

    if not username or not password:
        return jsonify({'error': 'Ad və şifrə tələb olunur'}), 400
    if role == 'superadmin':
        return jsonify({'error': 'Superadmin rolu əlavə edilə bilməz'}), 403

    users = load_users()
    if username in users:
        return jsonify({'error': 'Bu istifadəçi artıq mövcuddur'}), 400

    save_user(username, generate_password_hash(password), role, email)
    with db_conn() as conn:
        cur = conn.cursor()
        _upsert_user_data(cur, username, copy.deepcopy(DEFAULT_MENU_DATA))
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'add_user', {'username': username, 'role': role}, ip)
    email_sent = False
    if email and '@' in email:
        try:
            _send_password_reset_email(username, email)
            email_sent = True
        except Exception:
            import traceback
            print('[ADD USER RESET EMAIL ERROR]', traceback.format_exc())
    return jsonify({'ok': True, 'email_sent': email_sent})

@app.route('/api/users/<username>', methods=['DELETE'])
@superadmin_required
def api_delete_user(username):
    if username == current_user():
        return jsonify({'error': 'Özünüzü silə bilməzsiniz'}), 400
    delete_user(username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'delete_user', {'username': username}, ip)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/role', methods=['PUT'])
@superadmin_required
def api_update_user_role(username):
    data = request.json or {}
    role = data.get('role', 'manager')
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET role = %s WHERE username = %s", (role, username))
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'change_role', {'username': username, 'role': role}, ip)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/set-password', methods=['PUT'])
@login_required
def api_set_user_password(username):
    if session.get('role') != 'superadmin' and current_user() != username:
        return jsonify({'error': 'İcazə yoxdur'}), 403
    data = request.json or {}
    password = data.get('password', '')
    if not password or len(password) < 6:
        return jsonify({'error': 'Şifrə ən az 6 simvol olmalıdır'}), 400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password = %s, must_change_password = FALSE WHERE username = %s",
            (generate_password_hash(password), username)
        )
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'set_password', {'username': username}, ip)
    return jsonify({'ok': True})

@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    username = current_user()
    data = request.json or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    if not old_password or not new_password:
        return jsonify({'error': 'Köhnə və yeni şifrə tələb olunur'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Şifrə ən az 6 simvol olmalıdır'}), 400
    users = load_users()
    if username not in users or not check_password_hash(users[username]['password'], old_password):
        return jsonify({'error': 'Cari şifrə yanlışdır'}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password = %s, must_change_password = FALSE WHERE username = %s",
            (generate_password_hash(new_password), username)
        )
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(username, 'set_password', {'username': username}, ip)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/email', methods=['PUT'])
@login_required
def api_update_user_email(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET email = %s WHERE username = %s", (email, username))
    email_sent = False
    if email and '@' in email:
        try:
            _send_password_reset_email(username, email)
            email_sent = True
        except Exception:
            import traceback
            print('[EMAIL UPDATE RESET ERROR]', traceback.format_exc())
    return jsonify({'ok': True, 'email_sent': email_sent})

@app.route('/api/users/<username>/rename', methods=['PUT'])
@superadmin_required
def api_rename_user(username):
    import re
    data = request.json or {}
    new_username = data.get('new_username', '').strip().lower()
    if not new_username:
        return jsonify({'error': 'Yeni istifadəçi adı tələb olunur'}), 400
    if new_username == username:
        return jsonify({'ok': True})
    if not re.match(r'^[a-z0-9_]{3,30}$', new_username):
        return jsonify({'error': 'İstifadəçi adı 3-30 simvol, yalnız kiçik hərf/rəqəm/alt xətt'}), 400
    if username == current_user():
        return jsonify({'error': 'Öz adınızı dəyişə bilməzsiniz'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404
    if users[username].get('role') == 'superadmin':
        return jsonify({'error': 'Superadmin adını dəyişmək olmaz'}), 403
    if new_username in users:
        return jsonify({'error': 'Bu istifadəçi adı artıq mövcuddur'}), 400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET username = %s WHERE username = %s", (new_username, username))
        cur.execute("UPDATE user_data SET username = %s WHERE username = %s", (new_username, username))
    _invalidate_users_cache()
    _invalidate_cache(new_username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'rename_user', {'old': username, 'new': new_username}, ip)
    threading.Thread(target=_push_static_menu_async, args=(new_username,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/users/<username>/info', methods=['GET'])
@login_required
def api_get_user_info(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT username, email, role FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
    if not user:
        return jsonify({'error': 'Tapılmadı'}), 404
    return jsonify({'username': user['username'], 'email': user['email'] or '', 'role': user['role']})

# ────────────────────────────────────────────────────────────────
# STATİSTİKA — atomik JSONB yeniləmə
# ────────────────────────────────────────────────────────────────

_ALLOWED_STAT_TYPES = {'click', 'open', 'cat'}

@app.route('/api/stats', methods=['POST', 'OPTIONS'])
def api_track_stats():
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    data = request.json or {}
    username = data.get('user') or request.args.get('user') or current_user()
    if not username:
        return jsonify({'ok': False, 'error': 'user tələb olunur'}), 400

    stat_type = data.get('type', '')
    if stat_type not in _ALLOWED_STAT_TYPES:
        return jsonify({'ok': False, 'error': 'Keçərsiz stat növü'}), 400

    item_key = str(data.get('item') or data.get('cat') or '')[:120]
    today = datetime.now().strftime('%Y-%m-%d')

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        if not cur.fetchone():
            return jsonify({'ok': False, 'error': 'İstifadəçi tapılmadı'}), 404

        if stat_type == 'open':
            cur.execute("""
                UPDATE user_data SET data = jsonb_set(
                    jsonb_set(
                        data,
                        '{stats,opens,total}',
                        to_jsonb(COALESCE((data->'stats'->'opens'->>'total')::int, 0) + 1)
                    ),
                    ARRAY['stats','opens','dates',%s],
                    to_jsonb(COALESCE((data->'stats'->'opens'->'dates'->>%s)::int, 0) + 1)
                ) WHERE username = %s
            """, (today, today, username))
        elif stat_type == 'click':
            cur.execute("""
                UPDATE user_data SET data = jsonb_set(
                    data,
                    ARRAY['stats','clicks',%s],
                    to_jsonb(COALESCE((data->'stats'->'clicks'->>%s)::int, 0) + 1)
                ) WHERE username = %s
            """, (item_key, item_key, username))
        elif stat_type == 'cat':
            cur.execute("""
                UPDATE user_data SET data = jsonb_set(
                    data,
                    ARRAY['stats','cats',%s],
                    to_jsonb(COALESCE((data->'stats'->'cats'->>%s)::int, 0) + 1)
                ) WHERE username = %s
            """, (item_key, item_key, username))

    resp = jsonify({'ok': True})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/api/stats')
@login_required
def api_get_stats():
    db = load_user_data(current_user())
    return jsonify(db.get('stats', {'clicks': {}, 'opens': {'total': 0, 'dates': {}}, 'cats': {}}))

@app.route('/api/stats', methods=['DELETE'])
@login_required
def api_clear_stats():
    username = current_user()
    empty = json.dumps({'clicks': {}, 'opens': {'total': 0, 'dates': {}}, 'cats': {}})
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_data SET data = jsonb_set(data, '{stats}', %s::jsonb) WHERE username = %s",
            (empty, username)
        )
    return jsonify({'ok': True})

# ────────────────────────────────────────────────────────────────
# ŞİFRƏ SIFIRLAMA
# ────────────────────────────────────────────────────────────────

def _send_password_reset_email(username: str, recipient_email: str) -> None:
    """Token yaradır, DB-yə yazır, reset emaili göndərir. Xəta olsa raise edir."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=1)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM reset_tokens WHERE used = TRUE OR expires::timestamptz < NOW()")
        cur.execute(
            "INSERT INTO reset_tokens (token, username, expires, used) VALUES (%s,%s,%s,%s)",
            (token, username, expires, False)
        )
    reset_link = APP_BASE_URL + '/reset-password?token=' + token
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#FDF8F3;border-radius:16px">'
        '<h2 style="color:#C9A84C">QR Menu Admin</h2>'
        '<p>Salam <strong>' + username + '</strong>,</p>'
        '<p>Şifrə sıfırlama sorğusu alındı. Aşağıdakı düyməyə basın:</p>'
        '<p style="text-align:center;margin:28px 0">'
        '<a href="' + reset_link + '" style="background:#C9A84C;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:600">Şifrəni sıfırla</a>'
        '</p>'
        '<p style="color:#999;font-size:0.8rem">Bu link <strong>1 saat</strong> ərzində etibarlıdır.</p>'
        '</div>'
    )
    msg = Message(
        subject='QR Menu - Şifrə sıfırlama',
        recipients=[recipient_email],
        html=html_body,
        body='Şifrə sıfırlama linki: ' + reset_link
    )
    mail.send(msg)

@app.route('/api/forgot-password', methods=['POST'])
def api_forgot_password():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if _is_forgot_limited(ip):
        return jsonify({'error': 'Çox sayda cəhd. 5 dəqiqə gözləyin.'}), 429

    data = request.json or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'error': 'İstifadəçi adı daxil edin'}), 400

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT username, email FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        user = cur.fetchone()

    if not user:
        _record_forgot_attempt(ip)
        return jsonify({'error': 'Bu istifadəçi adı tapılmadı'}), 400

    recipient_email = (user['email'] or '').strip()
    if not recipient_email or '@' not in recipient_email:
        _record_forgot_attempt(ip)
        return jsonify({'error': 'Bu istifadəçiyə email təyin edilməyib. Superadmin ilə əlaqə saxlayın.'}), 400

    try:
        _send_password_reset_email(user['username'], recipient_email)
    except Exception as e:
        import traceback
        print('[FORGOT PASSWORD ERROR]', traceback.format_exc())
        return jsonify({'error': 'Xəta: ' + str(e)}), 500

    return jsonify({'ok': True, 'message': 'Şifrə sıfırlama linki emailinizə göndərildi'})

@app.route('/reset-password')
def reset_password_page():
    token = request.args.get('token', '')
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reset_tokens WHERE token = %s", (token,))
        td = cur.fetchone()

    if not td:
        return "<h2>❌ Keçərsiz link</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if td['used']:
        return "<h2>❌ Artıq istifadə edilib</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if td['expires'].replace(tzinfo=None) < datetime.now():
        return "<h2>⏰ Linkın vaxtı bitib</h2><a href='/admin'>Admin Panelə qayıt</a>"

    return redirect(f"/admin?reset_token={token}")

@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data = request.json or {}
    token = data.get('token', '').strip()
    new_password = data.get('password', '')

    if not token or not new_password:
        return jsonify({'error': 'Token və yeni şifrə tələb olunur'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Şifrə ən az 6 simvol olmalıdır'}), 400

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reset_tokens WHERE token = %s", (token,))
        td = cur.fetchone()

        if not td:
            return jsonify({'error': 'Keçərsiz link'}), 400
        if td['used']:
            return jsonify({'error': 'Bu link artıq istifadə olunub'}), 400
        if td['expires'].replace(tzinfo=None) < datetime.now():
            return jsonify({'error': 'Linkın vaxtı bitib'}), 400

        cur.execute(
            "UPDATE users SET password = %s, must_change_password = FALSE WHERE username = %s",
            (generate_password_hash(new_password), td['username'])
        )
        cur.execute("UPDATE reset_tokens SET used = TRUE WHERE token = %s", (token,))

    return jsonify({'ok': True, 'message': 'Şifrə uğurla yeniləndi'})

# ────────────────────────────────────────────────────────────────
# ONE-TIME: JSON MƏLUMATLARINI POSTGRESQL-Ə KÖÇ
# ────────────────────────────────────────────────────────────────

@app.route('/api/admin/import-data', methods=['POST'])
@superadmin_required
def api_import_data():
    data = request.get_json(force=True) or {}
    if not data:
        return jsonify({'error': 'JSON boşdur'}), 400

    MENU_KEYS = ('cafe', 'categories', 'items', 'theme', 'stats',
                 'customCss', 'font', 'layout', 'subscription')

    with db_conn() as conn:
        cur = conn.cursor()

        # 1. İstifadəçiləri əlavə et
        users = data.get('users', {})
        inserted_users = []
        for username, info in users.items():
            role = info.get('role', 'manager')
            if role not in ('superadmin', 'manager'):
                role = 'manager'
            password = info.get('password', '')
            email = info.get('email', '')
            cur.execute("""
                INSERT INTO users (username, password, role, email, must_change_password)
                VALUES (%s, %s, %s, %s, FALSE)
                ON CONFLICT (username) DO UPDATE
                    SET password = EXCLUDED.password, role = EXCLUDED.role,
                        email = EXCLUDED.email, must_change_password = FALSE
            """, (username, password, role, email))
            cur.execute("""
                INSERT INTO user_data (username, data) VALUES (%s, %s::jsonb)
                ON CONFLICT DO NOTHING
            """, (username, json.dumps(copy.deepcopy(DEFAULT_MENU_DATA), ensure_ascii=False)))
            inserted_users.append(f"{username} ({role})")

        # 2. Admin-in menyu datasını yaz
        menu_data = {k: data[k] for k in MENU_KEYS if k in data}
        cur.execute("""
            INSERT INTO user_data (username, data) VALUES ('admin', %s::jsonb)
            ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data
        """, (json.dumps(menu_data, ensure_ascii=False),))

    return jsonify({
        'ok': True,
        'users': inserted_users,
        'dataKeys': list(menu_data.keys()),
        'categories': len(data.get('categories', [])),
        'items': len(data.get('items', []))
    })

# ────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def serve_sw():
    resp = make_response(app.send_static_file('sw.js'))
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/health')
def health():
    try:
        with db_conn() as conn:
            conn.cursor().execute('SELECT 1')
        # Cache-ləri isti saxla: UptimeRobot hər 5 dəq ping edir
        users = load_users()
        for uname in users:
            if _get_cached_data(uname) is None:
                try:
                    _set_cached_data(uname, load_user_data(uname))
                except Exception:
                    pass
        return jsonify({'status': 'ok', 'db': 'connected', 'cached': len(users)})
    except Exception as e:
        return jsonify({'status': 'error', 'db': str(e)}), 500

@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'Fayl həcmi 5 MB-dan çox ola bilməz'}), 413

# ────────────────────────────────────────────────────────────────
# QR KOD
# ────────────────────────────────────────────────────────────────

@app.route('/api/qr')
@login_required
def api_qr():
    import qrcode
    username = current_user()
    url = f"{APP_BASE_URL}/menu/{username}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(username, 'qr_generate', {'url': url}, ip)
    return send_file(buf, mimetype='image/png',
                     download_name=f'{username}-menu-qr.png',
                     as_attachment=False)

# ────────────────────────────────────────────────────────────────
# EXPORT
# ────────────────────────────────────────────────────────────────

@app.route('/api/data/export')
@login_required
def api_export_data():
    username = current_user()
    db = load_user_data(username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(username, 'export', {}, ip)
    resp = make_response(json.dumps(db, ensure_ascii=False, indent=2))
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{username}-menu-export.json"'
    return resp

@app.route('/api/data/import', methods=['POST'])
@login_required
def api_restore_data():
    username = current_user()
    incoming = request.json
    if not isinstance(incoming, dict):
        return jsonify({'ok': False, 'error': 'Yanlış format'}), 400
    allowed_keys = {'cafe', 'categories', 'items', 'theme'}
    data = {k: incoming[k] for k in allowed_keys if k in incoming}
    if not data:
        return jsonify({'ok': False, 'error': 'Məlumat tapılmadı'}), 400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT data FROM user_data WHERE username = %s FOR UPDATE", (username,))
        row = cur.fetchone()
        db = row['data'] if row else copy.deepcopy(DEFAULT_MENU_DATA)
        db.update(data)
        _upsert_user_data(cur, username, db)
    _invalidate_cache(username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(username, 'import', {'keys': list(data.keys())}, ip)
    threading.Thread(target=_push_static_menu_async, args=(username,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/admin/export/<target_username>')
@superadmin_required
def api_admin_export_data(target_username):
    db = load_user_data(target_username)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    log_action(current_user(), 'export', {'username': target_username}, ip)
    resp = make_response(json.dumps(db, ensure_ascii=False, indent=2))
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{target_username}-menu-export.json"'
    return resp

# ────────────────────────────────────────────────────────────────
# SUPERADMİN — ÜMUMİ STATİSTİKA
# ────────────────────────────────────────────────────────────────

@app.route('/api/superadmin/stats')
@superadmin_required
def api_superadmin_stats():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.username,
                   COALESCE(ud.data->'cafe'->>'nameAz', u.username) AS name,
                   COALESCE((ud.data->'stats'->'opens'->>'total')::int, 0) AS opens,
                   ud.data->'stats'->'clicks' AS clicks,
                   ud.data->'stats'->'cats'   AS cats
            FROM users u
            LEFT JOIN user_data ud ON u.username = ud.username
            ORDER BY opens DESC
        """)
        rows = cur.fetchall()

    restaurants = []
    for r in rows:
        clicks = r['clicks'] or {}
        cats = r['cats'] or {}
        restaurants.append({
            'username': r['username'],
            'name': r['name'],
            'opens': r['opens'],
            'totalClicks': sum(int(v) for v in clicks.values()),
            'topItems': sorted(clicks.items(), key=lambda x: -int(x[1]))[:5],
            'topCats': sorted(cats.items(), key=lambda x: -int(x[1]))[:5],
        })

    return jsonify({
        'ok': True,
        'totalRestaurants': len(restaurants),
        'totalOpens': sum(r['opens'] for r in restaurants),
        'restaurants': restaurants
    })

# ────────────────────────────────────────────────────────────────
# AUDİT LOG
# ────────────────────────────────────────────────────────────────

@app.route('/api/admin/audit-log')
@superadmin_required
def api_audit_log():
    limit = min(int(request.args.get('limit', 100)), 500)
    user_filter = request.args.get('user', '').strip()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE ts < NOW() - INTERVAL '10 days'")
        if user_filter:
            cur.execute(
                "SELECT * FROM audit_log WHERE username = %s ORDER BY ts DESC LIMIT %s",
                (user_filter, limit)
            )
        else:
            cur.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    return jsonify([{
        'id': r['id'],
        'username': r['username'],
        'action': r['action'],
        'detail': r['detail'],
        'ip': r['ip'],
        'ts': r['ts'].isoformat()
    } for r in rows])

# ────────────────────────────────────────────────────────────────
# BAŞLANĞIC
# ────────────────────────────────────────────────────────────────

try:
    init_db()
except Exception as e:
    print(f"[init_db] Xəta: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
