#!/usr/bin/env python3
"""
Köhnə data.json faylındakı məlumatları PostgreSQL-ə köçürür.

İstifadə:
    python migrate.py [data.json yolu]

Əvvəlcə DATABASE_URL mühit dəyişənini təyin edin:
    $env:DATABASE_URL = "postgresql://..."   # Windows PowerShell
    export DATABASE_URL="postgresql://..."   # Linux/Mac
"""
import json, os, sys, copy
import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_MENU_DATA = {
    "cafe": {
        "nameAz": "Restoran", "nameEn": "Restaurant",
        "addrAz": "Bakı",     "addrEn": "Baku",
        "phone": "", "icon": "☕",
        "whatsapp": "", "instagram": "", "tiktok": "", "maps": ""
    },
    "categories": [],
    "items": [],
    "theme": {
        "id": "classic",
        "vars": {
            "accent": "#E8622A", "bg": "#FDF8F3", "card": "#FFFFFF",
            "text": "#1A1210",   "muted": "#8B7355",
            "border": "rgba(180,140,100,0.18)",
            "header": "#E8622A", "headerText": "#ffffff"
        }
    },
    "stats": {"clicks": {}, "opens": {"total": 0, "dates": {}}, "cats": {}}
}

def load_json(path):
    for enc in ('utf-8', 'utf-8-sig'):
        try:
            with open(path, encoding=enc) as f:
                return json.load(f)
        except Exception:
            continue
    raise ValueError(f"JSON oxumaq alınmadı: {path}")

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'data.json'

    if not os.path.exists(path):
        print(f"[XƏTA] Fayl tapılmadı: {path}")
        print("Lütfən data.json faylını bu qovluğa kopyalayın.")
        sys.exit(1)

    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print("[XƏTA] DATABASE_URL mühit dəyişəni təyin edilməyib.")
        print("Railway > layihəniz > Variables bölməsindən DATABASE_URL-i kopyalayın.")
        sys.exit(1)

    print(f"Oxunur: {path}")
    data = load_json(path)

    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    # ── 1. İSTİFADƏÇİLƏR ──────────────────────────────────────────
    users = data.get('users', {})
    inserted = []

    for username, info in users.items():
        role = info.get('role', 'manager')
        # 'admin' rolunu 'manager' kimi saxla (sistemdə yalnız superadmin/manager var)
        if role not in ('superadmin', 'manager'):
            role = 'manager'
        password = info.get('password', '')
        email = info.get('email', '')

        cur.execute("""
            INSERT INTO users (username, password, role, email, must_change_password)
            VALUES (%s, %s, %s, %s, FALSE)
            ON CONFLICT (username) DO UPDATE
                SET password             = EXCLUDED.password,
                    role                 = EXCLUDED.role,
                    email                = EXCLUDED.email,
                    must_change_password = FALSE
        """, (username, password, role, email))

        # user_data yoxdursa default data ilə yarat
        cur.execute("""
            INSERT INTO user_data (username, data)
            VALUES (%s, %s::jsonb)
            ON CONFLICT DO NOTHING
        """, (username, json.dumps(copy.deepcopy(DEFAULT_MENU_DATA), ensure_ascii=False)))

        inserted.append(f"{username} ({role})")

    # ── 2. ADMİNİN MENYU DATASI ────────────────────────────────────
    MENU_KEYS = ('cafe', 'categories', 'items', 'theme', 'stats',
                 'customCss', 'font', 'layout', 'subscription')
    menu_data = {k: data[k] for k in MENU_KEYS if k in data}

    cur.execute("""
        INSERT INTO user_data (username, data)
        VALUES ('admin', %s::jsonb)
        ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data
    """, (json.dumps(menu_data, ensure_ascii=False),))

    conn.commit()
    cur.close()
    conn.close()

    print("\n✓ Miqrasiya tamamlandı!")
    print(f"  İstifadəçilər  : {', '.join(inserted) if inserted else 'yoxdur'}")
    print(f"  Admin data     : {list(menu_data.keys())}")
    print(f"  Kateqoriya sayı: {len(data.get('categories', []))}")
    print(f"  Məhsul sayı    : {len(data.get('items', []))}")
    print()
    print("QEYD: Admin şifrəsi data.json-dakı köhnə şifrə ilə yeniləndi.")
    print("      Yeni şifrə ilə giriş etmək istəyirsinizsə, admin paneldən dəyişin.")

if __name__ == '__main__':
    main()
