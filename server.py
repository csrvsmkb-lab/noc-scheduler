#!/usr/bin/env python3
"""
ShiftCraft AI - Multi-user cloud server
Each manager has their own account and data.
Uses SQLite — no external database needed.
"""
import http.server
import json
import os
import sys
import sqlite3
import secrets
import hashlib
import time
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────
PORT     = int(os.environ.get('PORT', 10000))
DB_FILE  = Path(__file__).parent / 'noc.db'
FOLDER   = Path(__file__).parent
SESSION_TTL = 8 * 60 * 60  # 8 hours
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')  # Only this user can create accounts

# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token    TEXT PRIMARY KEY,
            user_id  INTEGER NOT NULL,
            created  REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS user_data (
            user_id  INTEGER PRIMARY KEY,
            data     TEXT NOT NULL,
            updated  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """)
    # Auto-create or update admin user from environment
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'admin1234')
    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
        if existing:
            # Update password in case it changed
            db.execute("UPDATE users SET password=? WHERE username=?",
                      (hash_password(admin_pass), ADMIN_USERNAME))
            print(f"  Admin password updated for: {ADMIN_USERNAME}")
        else:
            db.execute("INSERT INTO users (username, password) VALUES (?,?)",
                      (ADMIN_USERNAME, hash_password(admin_pass)))
            print(f"  Admin user created: {ADMIN_USERNAME}")
    print("  Database ready:", DB_FILE)

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def create_user(username, password):
    try:
        with get_db() as db:
            db.execute("INSERT INTO users (username, password) VALUES (?,?)",
                      (username, hash_password(password)))
        return True
    except sqlite3.IntegrityError:
        return False  # username taken

def check_user(username, password):
    with get_db() as db:
        row = db.execute("SELECT id FROM users WHERE username=? AND password=?",
                        (username, hash_password(password))).fetchone()
        return row['id'] if row else None

def user_exists(username):
    with get_db() as db:
        return db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() is not None

def create_session(user_id):
    token = secrets.token_hex(32)
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE created < ?", (time.time() - SESSION_TTL,))
        db.execute("INSERT INTO sessions (token, user_id, created) VALUES (?,?,?)",
                  (token, user_id, time.time()))
    return token

def get_session_user(token):
    if not token: return None
    with get_db() as db:
        row = db.execute(
            "SELECT user_id, created FROM sessions WHERE token=?", (token,)
        ).fetchone()
        if not row: return None
        if time.time() - row['created'] > SESSION_TTL:
            db.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
        return row['user_id']

def delete_session(token):
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))

def load_user_data(user_id):
    with get_db() as db:
        row = db.execute("SELECT data FROM user_data WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return json.loads(row['data'])
    return get_default_data()

def save_user_data(user_id, data):
    j = json.dumps(data, ensure_ascii=False)
    with get_db() as db:
        db.execute("""
            INSERT INTO user_data (user_id, data, updated)
            VALUES (?,?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=excluded.updated
        """, (user_id, j))

def get_default_data():
    return {
        'workers': [],
        'nextId': 1,
        'shifts': [
            {'name':'בוקר',   'start':'07:00','end':'15:00','color':'B'},
            {'name':'צהריים', 'start':'15:00','end':'23:00','color':'C'},
            {'name':'לילה',   'start':'23:00','end':'07:00','color':'L'},
        ],
        'constraints': {'wkmax':4,'consec':3,'rest':11,'nights':2,'cover':2},
        'lastSchedule': None,
        'lastStats': None,
        'lastGenerated': None,
    }

def count_users():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) as n FROM users").fetchone()['n']

# ─────────────────────────────────────────────────────────
#  HTTP HELPERS
# ─────────────────────────────────────────────────────────
MIME = {
    '.html':'text/html; charset=utf-8',
    '.js':'application/javascript',
    '.css':'text/css',
    '.json':'application/json',
    '.ico':'image/x-icon',
}

def read_body(req):
    length = int(req.headers.get('Content-Length', 0))
    return req.rfile.read(length).decode('utf-8') if length else ''

def get_token(req):
    for part in (req.headers.get('Cookie','') or '').split(';'):
        part = part.strip()
        if part.startswith('noc_session='):
            return part[len('noc_session='):]
    return None

# ─────────────────────────────────────────────────────────
#  HANDLER
# ─────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default logging

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        token = get_token(self)
        user_id = get_session_user(token)

        if path == '/api/me':
            if user_id:
                with get_db() as db:
                    row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
                username = row['username'] if row else ''
                return self.send_json(200, {'authed': True, 'username': username, 'isAdmin': username == ADMIN_USERNAME})
            return self.send_json(200, {'authed': False, 'isAdmin': False})

        if path == '/api/data':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            return self.send_json(200, load_user_data(user_id))

        # Static files
        fp = path if path != '/' else '/index.html'
        file_path = FOLDER / fp.lstrip('/')
        try:
            file_path.resolve().relative_to(FOLDER.resolve())
        except ValueError:
            self.send_response(403); self.end_headers(); return

        if file_path.exists() and file_path.is_file():
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', MIME.get(file_path.suffix, 'application/octet-stream'))
            self.send_header('Content-Length', len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()
            self.wfile.write(b'Not found')

    def do_POST(self):
        path = self.path.split('?')[0]
        token = get_token(self)
        user_id = get_session_user(token)
        body = read_body(self)
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            return self.send_json(400, {'error': 'Bad JSON'})

        # ── Register (admin only) ─────────────────────────
        if path == '/api/register':
            # Check if requester is admin
            req_user_id = get_session_user(token)
            is_admin = False
            if req_user_id:
                with get_db() as db:
                    row = db.execute("SELECT username FROM users WHERE id=?", (req_user_id,)).fetchone()
                    is_admin = row and row['username'] == ADMIN_USERNAME
            # Allow first user creation (no users yet) or admin
            if not is_admin and count_users() > 0:
                return self.send_json(403, {'error': 'רק מנהל המערכת יכול ליצור משתמשים'})
            username = payload.get('username','').strip()
            password = payload.get('password','')
            if not username or not password:
                return self.send_json(400, {'error': 'שם משתמש וסיסמה נדרשים'})
            if len(password) < 4:
                return self.send_json(400, {'error': 'סיסמה חייבת להיות לפחות 4 תווים'})
            if create_user(username, password):
                # If admin is creating, don't auto-login as new user
                if is_admin:
                    return self.send_json(200, {'ok': True, 'created': username})
                uid = check_user(username, password)
                t = create_session(uid)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', f'noc_session={t}; HttpOnly; Path=/; Max-Age={SESSION_TTL}')
                body_out = json.dumps({'ok': True}).encode()
                self.send_header('Content-Length', len(body_out))
                self.end_headers()
                self.wfile.write(body_out)
            else:
                self.send_json(409, {'error': 'שם המשתמש תפוס, נסה אחר'})
            return

        # ── Login ─────────────────────────────────────────
        if path == '/api/login':
            uid = check_user(payload.get('username',''), payload.get('password',''))
            if uid:
                t = create_session(uid)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', f'noc_session={t}; HttpOnly; Path=/; Max-Age={SESSION_TTL}')
                body_out = json.dumps({'ok': True}).encode()
                self.send_header('Content-Length', len(body_out))
                self.end_headers()
                self.wfile.write(body_out)
            else:
                self.send_json(401, {'error': 'שם משתמש או סיסמה שגויים'})
            return

        # ── List users (admin only) ───────────────────────
        if path == '/api/users':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = db.execute("SELECT username FROM users WHERE id=?", (req_user_id,)).fetchone()
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                users = db.execute("SELECT id, username, created FROM users ORDER BY created").fetchall()
                return self.send_json(200, {'users': [{'id':u['id'],'username':u['username'],'created':u['created']} for u in users]})

        # ── Delete user (admin only) ──────────────────────
        if path == '/api/delete-user':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = db.execute("SELECT username FROM users WHERE id=?", (req_user_id,)).fetchone()
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id = payload.get('user_id')
                target = db.execute("SELECT username FROM users WHERE id=?", (target_id,)).fetchone()
                if target and target['username'] == ADMIN_USERNAME:
                    return self.send_json(400, {'error': 'לא ניתן למחוק את מנהל המערכת'})
                db.execute("DELETE FROM user_data WHERE user_id=?", (target_id,))
                db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
                db.execute("DELETE FROM users WHERE id=?", (target_id,))
            return self.send_json(200, {'ok': True})

        # ── Logout ────────────────────────────────────────
        if path == '/api/logout':
            if token: delete_session(token)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie', 'noc_session=; HttpOnly; Path=/; Max-Age=0')
            body_out = json.dumps({'ok': True}).encode()
            self.send_header('Content-Length', len(body_out))
            self.end_headers()
            self.wfile.write(body_out)
            return

        # ── Protected routes ──────────────────────────────
        if not user_id:
            return self.send_json(401, {'error': 'לא מחובר'})

        if path == '/api/data':
            d = load_user_data(user_id)
            d.update({k:v for k,v in payload.items() if k != 'lastSchedule'})
            save_user_data(user_id, d)
            return self.send_json(200, {'ok': True})

        if path == '/api/save-schedule':
            d = load_user_data(user_id)
            d['lastSchedule']  = payload.get('schedule')
            d['lastStats']     = payload.get('stats')
            d['lastGenerated'] = datetime.now().isoformat()
            save_user_data(user_id, d)
            return self.send_json(200, {'ok': True})

        self.send_json(404, {'error': 'Not found'})

# ─────────────────────────────────────────────────────────
#  START
# ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'\n  ✅  ShiftCraft AI  →  http://localhost:{PORT}')
    print(f'  משתמשים רשומים: {count_users()}')
    print(f'  Database: {DB_FILE}')
    print(f'  לעצירה: Ctrl+C\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  השרת נסגר')
