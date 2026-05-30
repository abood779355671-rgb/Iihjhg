# 🤖 GitHub ZIP Uploader Bot

بوت تيليجرام يرفع ملفات ZIP مباشرة إلى GitHub بأداء محسّن.

---

## ⚡ تحسينات الأداء

| الميزة | الموقع الأصلي | البوت |
|--------|--------------|-------|
| طريقة الرفع | ملف واحد في كل مرة | Git Tree API (commit واحد) |
| التوازي | ❌ | ✅ 10 خيوط متوازية |
| سرعة 100 ملف | ~100 طلب | ~100 blob + 1 commit |
| دعم الملفات الكبيرة | محدود | حتى 100 MB |

---

## 🚀 التشغيل السريع

### 1. إنشاء البوت
1. افتح [@BotFather](https://t.me/BotFather) في تيليجرام
2. أرسل `/newbot` واتبع التعليمات
3. احفظ الـ Token

### 2. إعداد الملفات

```bash
# انسخ ملف الإعدادات
cp .env.example .env

# أضف التوكن
nano .env
```

### 3. التشغيل

#### بـ Docker (موصى به):
```bash
docker compose up -d

# مشاهدة السجلات
docker compose logs -f
```

#### بدون Docker:
```bash
pip install -r requirements.txt
python bot.py
```

---

## 📋 كيفية الاستخدام

1. افتح البوت في تيليجرام
2. أرسل `/start`
3. أدخل GitHub Token الخاص بك
4. أدخل اسم المستودع
5. اختر الخصوصية (عام/خاص)
6. أرسل ملف ZIP

---

## 🔐 إنشاء GitHub Token

1. اذهب إلى: `github.com/settings/tokens`
2. اضغط **Generate new token (classic)**
3. فعّل صلاحية: ✅ `repo`
4. اضغط **Generate token** وانسخ التوكن

---

## ⚙️ إعدادات متقدمة (.env)

```env
BOT_TOKEN=...          # توكن البوت (مطلوب)
MAX_CONCURRENT=10      # عدد الخيوط المتوازية
MAX_FILE_MB=100        # الحد الأقصى للحجم بالميجابايت
```

---

## 🛠 البنية التقنية

```
bot.py
├── GitHubClient      - تعامل مع GitHub API بشكل async
├── UploadEngine      - محرك الرفع
│   ├── upload_via_tree()     - Git Tree API (سريع)
│   └── upload_via_contents() - Contents API (احتياطي)
└── Conversation Handlers - إدارة محادثات تيليجرام
```
