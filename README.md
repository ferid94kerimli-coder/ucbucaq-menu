# Üçbucaq Restoran — Flask Admin Panel

## Quraşdırma

### 1. Python yükləyin
https://python.org/downloads (Windows üçün)

### 2. Lazımi kitabxanaları quraşdırın
```bash
pip install -r requirements.txt
```

### 3. Serveri işə salın
```bash
python app.py
```

### 4. Brauzerdə açın
- Admin panel: http://localhost:5000/admin
- Menyu:       http://localhost:5000/menu

## Giriş məlumatları
- İstifadəçi: `admin`
- Şifrə: `admin123`

> ⚠️ İlk girişdən sonra şifrənizi dəyişin!

## Fayl strukturu
```
ucbucaq/
  app.py              ← Python Flask server
  requirements.txt    ← Lazımi kitabxanalar
  data.json           ← Menyu məlumatları (hazır doldurulub)
  templates/
    admin.html        ← Admin panel
    menu.html         ← Müştəri menyusu
  static/
    uploads/          ← Yüklənmiş şəkillər
```

## Qovluq strukturunu qurun
```
mkdir templates static/uploads -p
mv admin.html menu.html templates/
```

## Xüsusiyyətlər
- ✅ 20 kateqoriya, 130+ menyu məhsulu hazır əlavə edilib
- ✅ Üçbucaq Restoran brend rəngləri (qızıl + tünd fon)
- ✅ Şəkil yükləmə (loqo + məhsul şəkilləri)
- ✅ Statistika (baxış, klik, kateqoriya)
- ✅ İstifadəçi idarəetməsi
- ✅ Azərbaycan / İngilis dil dəstəyi

## Deployment (Render.com — pulsuz)
1. GitHub-a yükləyin
2. render.com-da "New Web Service"
3. Repository seçin, `python app.py` start command
4. Deploy edin