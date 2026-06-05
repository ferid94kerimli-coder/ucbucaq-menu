from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_from_directory
from functools import wraps
import json, os, uuid, secrets
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from PIL import Image
import io

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ucbucaq-restoran-secret-2025')

# ── EMAIL KONFİQURASİYASI ──
# Mühit dəyişənlərini .env faylından və ya server konfiqürasiyasından oxuyun.
# Heç vaxt şifrəni birbaşa koda yazmayın!
_mail_user = os.environ.get('MAIL_USERNAME')
_mail_pass = os.environ.get('MAIL_PASSWORD')

if not _mail_user or not _mail_pass:
    import warnings
    warnings.warn(
        "MAIL_USERNAME və ya MAIL_PASSWORD mühit dəyişəni təyin edilməyib. "
        "Email funksiyası işləməyəcək. Zəhmət olmasa .env faylını yoxlayın.",
        RuntimeWarning
    )
app.config['MAIL_SERVER']         = 'smtp.gmail.com'
app.config['MAIL_PORT']           = 465
app.config['MAIL_USE_TLS']        = False
app.config['MAIL_USE_SSL']        = True
app.config['MAIL_USERNAME']       = _mail_user
app.config['MAIL_PASSWORD']       = _mail_pass
app.config['MAIL_DEFAULT_SENDER'] = ('QR Menu', _mail_user)
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'https://ucbucaqq-production.up.railway.app')

mail = Mail(app)

# ── QOVLUQLAR ──
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
USERS_FILE      = os.path.join(BASE_DIR, 'users.json')        # İstifadəçi hesabları
USER_DATA_DIR   = os.path.join(BASE_DIR, 'user_data')         # Hər userin öz data qovluğu
UPLOAD_DIR      = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXT     = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

# ────────────────────────────────────────────────────────────────
# İSTİFADƏÇİ HESABLARI  (users.json — yalnız login məlumatları)
# ────────────────────────────────────────────────────────────────
DEFAULT_USERS = {
    "admin": {
        "password": generate_password_hash("admin123"),
        "role": "superadmin",
        "email": ""
    }
}

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    save_users(DEFAULT_USERS)
    return DEFAULT_USERS.copy()

def save_users(users):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

# ────────────────────────────────────────────────────────────────
# HƏR USERİN ÖZ DATA FAYLI  (user_data/<username>.json)
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

def user_data_file(username):
    """Hər userin öz data faylının yolu."""
    safe = secure_filename(username)          # qovluq traversal-dan qoruma
    return os.path.join(USER_DATA_DIR, f"{safe}.json")

def load_user_data(username):
    path = user_data_file(username)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    # İlk giriş — default data ilə başla
    import copy
    data = copy.deepcopy(DEFAULT_MENU_DATA)
    save_user_data(username, data)
    return data

def save_user_data(username, data):
    path = user_data_file(username)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── KÖMƏKÇİ ──
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

# Şəkil ölçüsünü kiçiltmə köməkçisi
# max_size: (en, hündürlük) piksel — böyüksə azaldılır, kiçiksə dəyişdirilmir
# quality: JPEG/WEBP sıxışdırma keyfiyyəti (1-95)
def resize_and_save(file_obj, save_path, max_size=(1200, 1200), quality=82):
    img = Image.open(file_obj)
    # EXIF orientasiyasını düzəlt (telefon şəkilləri üçün)
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
    # Ölçünü azalt (nisbəti qoru)
    img.thumbnail(max_size, Image.LANCZOS)
    # GIF-ləri olduğu kimi saxla
    ext = os.path.splitext(save_path)[1].lower()
    if ext == '.gif':
        img.save(save_path)
        return
    # PNG-ləri RGB-yə çevir (şəffaflıq varsa saxla)
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        save_ext = '.png'
        img.save(save_path.rsplit('.', 1)[0] + save_ext, 'PNG', optimize=True)
        return
    if img.mode != 'RGB':
        img = img.convert('RGB')
    # WEBP və ya JPEG kimi saxla
    fmt = 'WEBP' if ext == '.webp' else 'JPEG'
    img.save(save_path, fmt, quality=quality, optimize=True)

def current_user():
    return session.get('user')

# ────────────────────────────────────────────────────────────────
# AUTH DEKORATORları
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
            return jsonify({'error': 'Bu əməliyyat üçün superadmin səlahiyyəti lazımdır'}), 403
        return f(*args, **kwargs)
    return decorated

# ────────────────────────────────────────────────────────────────
# SƏHIFƏLƏR
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
# DATA API  — hər user yalnız öz datasını görür/dəyişir
# ────────────────────────────────────────────────────────────────
@app.route('/api/data')
def api_get_data():
    """
    Menyu səhifəsi üçün ictimai endpoint.
    URL-dən ?user=<username> keçilə bilər; keçilməzsə oturum istifadəçisi götürülür.
    """
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
    """Yalnız giriş etmiş istifadəçi öz datasını dəyişə bilər."""
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
# ŞƏKIL YÜKLƏMƏ
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
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = str(uuid.uuid4()) + '.' + ext
    save_path = os.path.join(UPLOAD_DIR, filename)
    try:
        resize_and_save(file, save_path, max_size=(1200, 1200), quality=82)
    except Exception:
        # Pillow oxuya bilməsə orijinalı saxla
        file.seek(0)
        file.save(save_path)
    url = '/static/uploads/' + filename
    return jsonify({'ok': True, 'url': url})

@app.route('/api/upload/logo', methods=['POST'])
@login_required
def api_upload_logo():
    if 'file' not in request.files:
        return jsonify({'error': 'Fayl tapılmadı'}), 400
    file = request.files['file']
    if not allowed_file(file.filename):
        return jsonify({'error': 'Yalnız şəkil faylları'}), 400
    ext = file.filename.rsplit('.', 1)[1].lower()
    # Hər userin loqosu ayrı saxlanır
    username = current_user()
    filename = f'logo_{secure_filename(username)}.{ext}'
    save_path = os.path.join(UPLOAD_DIR, filename)
    try:
        resize_and_save(file, save_path, max_size=(400, 400), quality=85)
    except Exception:
        file.seek(0)
        file.save(save_path)
    url = '/static/uploads/' + filename + '?v=' + str(int(datetime.now().timestamp()))
    db = load_user_data(username)
    db['cafe']['logo'] = url
    save_user_data(username, db)
    return jsonify({'ok': True, 'url': url})

# ────────────────────────────────────────────────────────────────
# İSTİFADƏÇİ İDARƏETMƏSİ  (yalnız superadmin)
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
    role     = data.get('role', 'manager')
    email    = data.get('email', '').strip()
    if not username or not password:
        return jsonify({'error': 'Ad və şifrə tələb olunur'}), 400
    if role == 'superadmin':
        return jsonify({'error': 'Superadmin rolu əlavə edilə bilməz'}), 403
    users = load_users()
    if username in users:
        return jsonify({'error': 'Bu istifadəçi artıq mövcuddur'}), 400
    users[username] = {'password': generate_password_hash(password), 'role': role, 'email': email}
    save_users(users)
    # Yeni userin boş data faylını yarad
    import copy
    save_user_data(username, copy.deepcopy(DEFAULT_MENU_DATA))
    return jsonify({'ok': True})

@app.route('/api/users/<username>', methods=['DELETE'])
@superadmin_required
def api_delete_user(username):
    if username == current_user():
        return jsonify({'error': 'Özünüzü silə bilməzsiniz'}), 400
    users = load_users()
    users.pop(username, None)
    save_users(users)
    # İstəsəniz data faylını da silin:
    path = user_data_file(username)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/role', methods=['PUT'])
@superadmin_required
def api_update_user_role(username):
    data = request.json or {}
    role = data.get('role', 'manager')
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404
    users[username]['role'] = role
    save_users(users)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/set-password', methods=['PUT'])
@login_required
def api_set_user_password(username):
    # Superadmin hər kəsin şifrəsini dəyişə bilər; özü isə yalnız özününkü
    if session.get('role') != 'superadmin' and current_user() != username:
        return jsonify({'error': 'İcazə yoxdur'}), 403
    data = request.json or {}
    password = data.get('password', '')
    if not password or len(password) < 6:
        return jsonify({'error': 'Şifrə ən az 6 simvol olmalıdır'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404
    users[username]['password'] = generate_password_hash(password)
    save_users(users)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/email', methods=['PUT'])
@login_required
def api_update_user_email(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 404
    users[username]['email'] = email
    save_users(users)
    return jsonify({'ok': True})

@app.route('/api/users/<username>/info', methods=['GET'])
@login_required
def api_get_user_info(username):
    if username != current_user() and session.get('role') != 'superadmin':
        return jsonify({'error': 'İcazə yoxdur'}), 403
    users = load_users()
    user = users.get(username)
    if not user:
        return jsonify({'error': 'Tapılmadı'}), 404
    return jsonify({'username': username, 'email': user.get('email', ''), 'role': user.get('role', 'manager')})

# ────────────────────────────────────────────────────────────────
# STATİSTİKA  — hər userin öz statistikası
# ────────────────────────────────────────────────────────────────
@app.route('/api/stats', methods=['POST'])
def api_track_stats():
    data = request.json or {}
    # Menyu səhifəsindən ?user= ilə kim göndərdi bilirik
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

    users = load_users()

    # Username ilə tap (case-insensitive)
    matched_user = None
    for uname in users:
        if uname.lower() == username.lower():
            matched_user = uname
            break

    if not matched_user:
        return jsonify({'error': 'Bu istifadəçi adı tapılmadı'}), 400

    recipient_email = users[matched_user].get('email', '').strip()
    if not recipient_email or '@' not in recipient_email:
        return jsonify({'error': 'Bu istifadəçiyə email təyin edilməyib. Superadmin ilə əlaqə saxlayın.'}), 400

    try:
        token   = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(hours=1)).isoformat()

        reset_file = os.path.join(BASE_DIR, 'reset_tokens.json')
        try:
            with open(reset_file, 'r', encoding='utf-8') as rf:
                reset_tokens = json.load(rf)
        except Exception:
            reset_tokens = {}
        reset_tokens[token] = {'username': matched_user, 'expires': expires, 'used': False}
        with open(reset_file, 'w', encoding='utf-8') as rf:
            json.dump(reset_tokens, rf, ensure_ascii=False, indent=2)

        reset_link = APP_BASE_URL + '/reset-password?token=' + token
        html_body = (
            '<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#FDF8F3;border-radius:16px">'
            '<h2 style="color:#C9A84C">QR Menu Admin</h2>'
            '<p>Salam <strong>' + matched_user + '</strong>,</p>'
            '<p>Şifrə sıfırlama sorğusu alındı. Aşağıdakı düyməyə basın:</p>'
            '<p style="text-align:center;margin:28px 0">'
            '<a href="' + reset_link + '" style="background:#C9A84C;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:600">Şifrəni sıfırla</a>'
            '</p>'
            '<p style="color:#999;font-size:0.8rem">Bu link <strong>1 saat</strong> erzinde etibarlıdır.</p>'
            '</div>'
        )
        msg = Message(
            subject='QR Menu - Sifre sifirlama',
            recipients=[recipient_email],
            html=html_body,
            body='Sifre sifirlama linki: ' + reset_link
        )
        mail.send(msg)
    except Exception as e:
        import traceback
        print('[FORGOT PASSWORD ERROR]', traceback.format_exc())
        return jsonify({'error': 'Xeta: ' + str(e)}), 500

    return jsonify({'ok': True, 'message': 'Sifre sifirlama linki emailinize gonderildi'})


@app.route('/reset-password')
def reset_password_page():
    token = request.args.get('token', '')
    reset_file = os.path.join(BASE_DIR, 'reset_tokens.json')
    try:
        with open(reset_file, 'r', encoding='utf-8') as f:
            reset_tokens = json.load(f)
    except Exception:
        reset_tokens = {}
    token_data = reset_tokens.get(token)
    if not token_data:
        return "<h2>❌ Keçərsiz link</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if token_data.get('used'):
        return "<h2>❌ Artıq istifadə edilib</h2><a href='/admin'>Admin Panelə qayıt</a>"
    if datetime.fromisoformat(token_data['expires']) < datetime.now():
        return "<h2>⏰ Linkın vaxtı bitib</h2><a href='/admin'>Admin Panelə qayıt</a>"
    return redirect(f"/admin?reset_token={token}")


@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data         = request.json or {}
    token        = data.get('token', '').strip()
    new_password = data.get('password', '')

    if not token or not new_password:
        return jsonify({'error': 'Token və yeni şifrə tələb olunur'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Şifrə ən az 6 simvol olmalıdır'}), 400

    reset_file = os.path.join(BASE_DIR, 'reset_tokens.json')
    try:
        with open(reset_file, 'r', encoding='utf-8') as f:
            reset_tokens = json.load(f)
    except Exception:
        reset_tokens = {}

    token_data = reset_tokens.get(token)
    if not token_data:
        return jsonify({'error': 'Keçərsiz link'}), 400
    if token_data.get('used'):
        return jsonify({'error': 'Bu link artıq istifadə olunub'}), 400
    if datetime.fromisoformat(token_data['expires']) < datetime.now():
        return jsonify({'error': 'Linkın vaxtı bitib'}), 400

    username = token_data['username']
    users = load_users()
    if username not in users:
        return jsonify({'error': 'İstifadəçi tapılmadı'}), 400

    users[username]['password'] = generate_password_hash(new_password)
    save_users(users)

    reset_tokens[token]['used'] = True
    with open(reset_file, 'w', encoding='utf-8') as f:
        json.dump(reset_tokens, f, ensure_ascii=False, indent=2)

    return jsonify({'ok': True, 'message': 'Şifrə uğurla yeniləndi'})


# ────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────
# MİQRASİYA: köhnə data.json-u hər userin öz faylına köçür
# ────────────────────────────────────────────────────────────────
def migrate_legacy():
    """
    Köhnə tək fayllı data.json varsa:
      - users.json yarat
      - hər user üçün user_data/<user>.json yarat
      - data.json-u data.json.migrated-ə adlandır (bir daha oxunmasın)
    """
    legacy   = os.path.join(BASE_DIR, 'data.json')
    migrated = os.path.join(BASE_DIR, 'data.json.migrated')

    # Əgər köhnə fayl yoxdursa ya miqrasiya edilibsə — keç
    if not os.path.exists(legacy) or os.path.exists(migrated):
        return

    print("[migrate] Köhnə data.json tapıldı, miqrasiya başlayır...")
    try:
        with open(legacy, 'r', encoding='utf-8') as f:
            old = json.load(f)
    except Exception as e:
        print(f"[migrate] oxunmadı: {e}")
        return

    # users.json yarat (yoxdursa)
    if not os.path.exists(USERS_FILE):
        raw_users = old.pop('users', {})
        new_users = {}
        for uname, uinfo in raw_users.items():
            new_users[uname] = {
                'password': uinfo.get('password', generate_password_hash('admin123')),
                'role':     uinfo.get('role', 'manager'),
                'email':    uinfo.get('email', '')
            }
        if not new_users:
            import copy as _copy
            new_users = _copy.deepcopy(DEFAULT_USERS)
        save_users(new_users)
        print(f"[migrate] users.json yaradıldı: {list(new_users.keys())}")
    else:
        old.pop('users', None)
        new_users = load_users()

    # Superadmin üçün mövcud datanı köçür
    admin_name = next(
        (u for u, i in new_users.items() if i.get('role') == 'superadmin'),
        list(new_users.keys())[0]
    )
    admin_path = user_data_file(admin_name)
    if not os.path.exists(admin_path):
        import copy as _copy
        base = _copy.deepcopy(DEFAULT_MENU_DATA)
        for k in ('cafe', 'categories', 'items', 'theme', 'stats'):
            if k in old:
                base[k] = old[k]
        old.pop('reset_tokens', None)
        save_user_data(admin_name, base)
        print(f"[migrate] user_data/{admin_name}.json yaradıldı (köhnə məlumatlarla)")

    # Digər mövcud userlər üçün boş fayl yarat
    for uname in new_users:
        if uname != admin_name:
            upath = user_data_file(uname)
            if not os.path.exists(upath):
                import copy as _copy
                save_user_data(uname, _copy.deepcopy(DEFAULT_MENU_DATA))
                print(f"[migrate] user_data/{uname}.json yaradıldı (boş)")

    # data.json-u arxivlə — bir daha oxunmasın
    os.rename(legacy, migrated)
    print("[migrate] Tamamlandı — data.json.migrated olaraq arxivləndi")

migrate_legacy()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
