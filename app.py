from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from functools import wraps
import json, os, uuid, secrets
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from PIL import Image
import io
import psycopg2
from psycopg2.extras import RealDictCursor
import cloudinary
import cloudinary.uploader

app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get('SECRET_KEY', 'ucbucaq-restoran-secret-2025')

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
# PostgreSQL BAĞLANTISI
# ────────────────────────────────────────────────────────────────

def get_db():
    """Hər request üçün yeni DB bağlantısı açır."""
    conn = psycopg2.connect(
        os.environ.get('DATABASE_URL'),
        cursor_factory=RealDictCursor
    )
    return conn

def init_db():
    """Cədvəlləri yarat (mövcuddursa keç)."""
    conn = get_db()
    cur = conn.cursor()

    # İstifadəçilər cədvəli
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username    TEXT PRIMARY KEY,
            password    TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'manager',
            email       TEXT DEFAULT ''
        )
    """)

    # Hər userin menyu datası
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            username    TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            data        JSONB NOT NULL DEFAULT '{}'
        )
    """)

    # Şifrə sıfırlama tokenləri
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reset_tokens (
            token       TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            expires     TIMESTAMP NOT NULL,
            used        BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)

    # Default superadmin yarat (yoxdursa)
    cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password, role, email) VALUES (%s, %s, %s, %s)",
            ('admin', generate_password_hash('admin123'), 'superadmin', '')
        )
        cur.execute(
            "INSERT INTO user_data (username, data) VALUES (%s, %s)",
            ('admin', json.dumps(DEFAULT_MENU_DATA))
        )

    conn.commit()
    cur.close()
    conn.close()

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
# İSTİFADƏÇİ ƏMƏLIYYATLARI (PostgreSQL)
# ────────────────────────────────────────────────────────────────

def load_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, password, role, email FROM users")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r['username']: {'password': r['password'], 'role': r['role'], 'email': r['email'] or ''} for r in rows}

def save_user(username, password_hash, role, email=''):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users (username, password, role, email) VALUES (%s,%s,%s,%s)
           ON CONFLICT (username) DO UPDATE SET password=EXCLUDED.password, role=EXCLUDED.role, email=EXCLUDED.email""",
        (username, password_hash, role, email)
    )
    conn.commit()
    cur.close()
    conn.close()

def delete_user(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE username = %s", (username,))
    conn.commit()
    cur.close()
    conn.close()

# ────────────────────────────────────────────────────────────────
# MENYU DATA ƏMƏLIYYATLARI (PostgreSQL JSONB)
# ────────────────────────────────────────────────────────────────

def load_user_data(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT data FROM user_data WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return row['data']
    # İlk dəfə — default data yarat
    import copy
    data = copy.deepcopy(DEFAULT_MENU_DATA)
    save_user_data(username, data)
    return data

def save_user_data(username, data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO user_data (username, data) VALUES (%s, %s)
           ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data""",
        (username, json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    cur.close()
    conn.close()

# ────────────────────────────────────────────────────────────────
# KÖMƏKÇİLƏR
# ────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def current_user():
    return session.get('user')

def resize_image(file_obj, max_size=(1200, 1200), quality=82):
    """Pillow ilə ölçünü kiçilt, bytes olaraq qaytar."""
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
    """Faylı Cloudinary-ə yüklə, URL qaytar."""
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
    return render_template('menu.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

# ────────────────────────────────────────────────────────────────
# AUTH API
# ────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    users = load_users()
    if username in users and check_password_hash(users[username]['password'], password):
        role = users[username].get('role', 'manager')
        session['user'] = username
        session['role'] = role
        return jsonify({'ok': True, 'username': username, 'role': role})
    return jsonify({'ok': False, 'error': 'İstifadəçi adı və ya şifrə yanlışdır'}), 401

@app.route('/api/logout', methods=['POST'])
def api_logout():
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
    username = request.args.get('user') or current_user()
    if not username:
        return jsonify({'error': 'İstifadəçi müəyyən edilmədi'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404
    db = load_user_data(username)
    return jsonify(db)

@app.route('/api/data', methods=['PUT'])
@login_required
def api_save_data():
    username = current_user()
    incoming = request.json
    db = load_user_data(username)
    allowed_keys = {'cafe', 'categories', 'items', 'theme'}
    for key in incoming:
        if key in allowed_keys:
            db[key] = incoming[key]
    save_user_data(username, db)
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
        public_id = f"ucbucaq/items/{uuid.uuid4()}"
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
        public_id = f"ucbucaq/logos/{secure_filename(username)}"
        url = upload_to_cloudinary(file, public_id=public_id, max_size=(400, 400))
        # Logo URL-ni DB-yə yaz
        db = load_user_data(username)
        db['cafe']['logo'] = url
        save_user_data(username, db)
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
    data = request.json
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

    import copy
    save_user_data(username, copy.deepcopy(DEFAULT_MENU_DATA))
    return jsonify({'ok': True})

@app.route('/api/users/<username>', methods=['DELETE'])
@superadmin_required
def api_delete_user(username):
    if username == current_user():
        return jsonify({'error': 'Özünüzü silə bilməzsiniz'}), 400
    delete_user(username)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/role', methods=['PUT'])
@superadmin_required
def api_update_user_role(username):
    data = request.json or {}
    role = data.get('role', 'manager')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = %s WHERE username = %s", (role, username))
    conn.commit()
    cur.close()
    conn.close()
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password = %s WHERE username = %s",
                (generate_password_hash(password), username))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<username>/email', methods=['PUT'])
@login_required
def api_update_user_email(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET email = %s WHERE username = %s", (email, username))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<username>/info', methods=['GET'])
@login_required
def api_get_user_info(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, email, role FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return jsonify({'error': 'Tapılmadı'}), 404
    return jsonify({'username': user['username'], 'email': user['email'] or '', 'role': user['role']})

# ────────────────────────────────────────────────────────────────
# STATİSTİKA
# ────────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['POST'])
def api_track_stats():
    data = request.json or {}
    username = data.get('user') or request.args.get('user') or current_user()
    if not username:
        return jsonify({'ok': False, 'error': 'user tələb olunur'}), 400

    db = load_user_data(username)
    stats = db.setdefault('stats', {'clicks': {}, 'opens': {'total': 0, 'dates': {}}, 'cats': {}})

    if data.get('type') == 'click':
        key = data.get('item', '')
        stats['clicks'][key] = stats['clicks'].get(key, 0) + 1
    elif data.get('type') == 'open':
        stats['opens']['total'] = stats['opens'].get('total', 0) + 1
        today = datetime.now().strftime('%Y-%m-%d')
        stats['opens']['dates'][today] = stats['opens']['dates'].get(today, 0) + 1
    elif data.get('type') == 'cat':
        key = data.get('cat', '')
        stats['cats'][key] = stats['cats'].get(key, 0) + 1

    save_user_data(username, db)
    return jsonify({'ok': True})

@app.route('/api/stats')
@login_required
def api_get_stats():
    db = load_user_data(current_user())
    return jsonify(db.get('stats', {'clicks': {}, 'opens': {'total': 0, 'dates': {}}, 'cats': {}}))

@app.route('/api/stats', methods=['DELETE'])
@login_required
def api_clear_stats():
    username = current_user()
    db = load_user_data(username)
    db['stats'] = {'clicks': {}, 'opens': {'total': 0, 'dates': {}}, 'cats': {}}
    save_user_data(username, db)
    return jsonify({'ok': True})

# ────────────────────────────────────────────────────────────────
# ŞİFRƏ SIFIRLAMA
# ────────────────────────────────────────────────────────────────

@app.route('/api/forgot-password', methods=['POST'])
def api_forgot_password():
    data = request.json or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'error': 'İstifadəçi adı daxil edin'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, email FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        return jsonify({'error': 'Bu istifadəçi adı tapılmadı'}), 400

    recipient_email = (user['email'] or '').strip()
    if not recipient_email or '@' not in recipient_email:
        return jsonify({'error': 'Bu istifadəçiyə email təyin edilməyib. Superadmin ilə əlaqə saxlayın.'}), 400

    try:
        token = secrets.token_urlsafe(32)
        expires = datetime.now() + timedelta(hours=1)

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reset_tokens (token, username, expires, used) VALUES (%s,%s,%s,%s)",
            (token, user['username'], expires, False)
        )
        conn.commit()
        cur.close()
        conn.close()

        reset_link = APP_BASE_URL + '/reset-password?token=' + token
        html_body = (
            '<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#FDF8F3;border-radius:16px">'
            '<h2 style="color:#C9A84C">QR Menu Admin</h2>'
            '<p>Salam <strong>' + user['username'] + '</strong>,</p>'
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
    except Exception as e:
        import traceback
        print('[FORGOT PASSWORD ERROR]', traceback.format_exc())
        return jsonify({'error': 'Xəta: ' + str(e)}), 500

    return jsonify({'ok': True, 'message': 'Şifrə sıfırlama linki emailinizə göndərildi'})

@app.route('/reset-password')
def reset_password_page():
    token = request.args.get('token', '')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM reset_tokens WHERE token = %s", (token,))
    td = cur.fetchone()
    cur.close()
    conn.close()

    if not td:
        return "<h2>❌ Keçərsiz link</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if td['used']:
        return "<h2>❌ Artıq istifadə edilib</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if td['expires'] < datetime.now():
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

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM reset_tokens WHERE token = %s", (token,))
    td = cur.fetchone()

    if not td:
        cur.close(); conn.close()
        return jsonify({'error': 'Keçərsiz link'}), 400
    if td['used']:
        cur.close(); conn.close()
        return jsonify({'error': 'Bu link artıq istifadə olunub'}), 400
    if td['expires'] < datetime.now():
        cur.close(); conn.close()
        return jsonify({'error': 'Linkın vaxtı bitib'}), 400

    cur.execute("UPDATE users SET password = %s WHERE username = %s",
                (generate_password_hash(new_password), td['username']))
    cur.execute("UPDATE reset_tokens SET used = TRUE WHERE token = %s", (token,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'ok': True, 'message': 'Şifrə uğurla yeniləndi'})

# ────────────────────────────────────────────────────────────────
# BAŞLANĞIC
# ────────────────────────────────────────────────────────────────

# DB cədvəllərini yarat
try:
    init_db()
except Exception as e:
    print(f"[init_db] Xəta: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
