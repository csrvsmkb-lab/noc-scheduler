# NOC Scheduler — Cloud Version

## פריסה ב-Railway (חינמי)

### שלב 1 — GitHub
1. כנס ל: https://github.com
2. צור חשבון חינמי (אם אין)
3. לחץ "New repository" → שם: `noc-scheduler` → Public → Create
4. גרור את כל הקבצים מהתיקייה לדף ה-repository

### שלב 2 — Railway
1. כנס ל: https://railway.app
2. התחבר עם חשבון GitHub
3. לחץ "New Project" → "Deploy from GitHub repo"
4. בחר את `noc-scheduler`
5. Railway יפרוס אוטומטית

### שלב 3 — קבל URL
1. ב-Railway לחץ על הפרויקט → Settings → Networking
2. לחץ "Generate Domain"
3. תקבל כתובת כמו: `https://noc-scheduler-xxx.railway.app`

### שלב 4 — שתף
שלח את הכתובת לכל מנהל.
כל מנהל נרשם בעצמו דרך "הרשמה" ויוצר חשבון עצמאי.

## מבנה
- server.py    — שרת Python + SQLite
- index.html   — ממשק משתמש
- noc.db       — נתונים (נוצר אוטומטית)

## אבטחה
- כל מנהל רואה רק את הנתונים שלו
- סיסמאות מוצפנות (SHA-256)
- Session cookies מוגנים

## הרצה מקומית
  python server.py
  פתח: http://localhost:3000
