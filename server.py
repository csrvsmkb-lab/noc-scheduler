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
import secrets
import hashlib
import time
from pathlib import Path
from datetime import datetime

# PostgreSQL support
try:
    import psycopg2
    import psycopg2.extras
    USE_PG = True
except ImportError:
    import sqlite3
    USE_PG = False

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────
PORT     = int(os.environ.get('PORT', 10000))
DB_FILE   = Path(__file__).parent / 'noc.db'
FOLDER    = Path(__file__).parent
DATABASE_URL = os.environ.get('DATABASE_URL', '')
SESSION_TTL = 8 * 60 * 60  # 8 hours
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')  # Only this user can create accounts

# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────
def get_db():
    if USE_PG and DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def is_pg():
    return USE_PG and bool(DATABASE_URL)

def execute_sql(db, sql):
    if is_pg():
        cur = db.cursor()
        # Split and run each statement
        for stmt in sql.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                # Convert SQLite syntax to PostgreSQL
                stmt = stmt.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
                stmt = stmt.replace("datetime('now')", 'NOW()')
                stmt = stmt.replace('CREATE TABLE IF NOT EXISTS _migrate_done (id INTEGER PRIMARY KEY)', '')
                if stmt.strip():
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        if 'already exists' not in str(e):
                            print(f'SQL warning: {e}')
        db.commit()
    else:
        db.executescript(sql)

def fetchone(db, sql, params=()):
    if is_pg():
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchone()
    return fetchone(db, sql, params)

def fetchall(db, sql, params=()):
    if is_pg():
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()
    return fetchall(db, sql, params)

def execute(db, sql, params=()):
    if is_pg():
        cur = db.cursor()
        cur.execute(sql, params)
        db.commit()
        return cur
    return execute(db, sql, params)

def init_db():
    # Reset DB if requested
    if not is_pg() and os.environ.get('RESET_DB') == '1' and DB_FILE.exists():
        DB_FILE.unlink()
        print("  Database reset!")
    with get_db() as db:
        sql = """
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created  TEXT DEFAULT (datetime('now'))
        );
        -- Add is_admin column if upgrading from old db
        CREATE TABLE IF NOT EXISTS _migrate_done (id INTEGER PRIMARY KEY);
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
        """
        execute_sql(db, sql)
    # Migrate: add is_admin column if missing
    try:
        with get_db() as db:
            execute(db, "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
            print("  Migrated: added is_admin column")
    except Exception:
        pass  # Column already exists

    # Auto-create or update admin user from environment
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'admin1234')
    with get_db() as db:
        existing = fetchone(db, "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))
        if existing:
            execute(db, "UPDATE users SET password=?, is_admin=1 WHERE username=?",
                      (hash_password(admin_pass), ADMIN_USERNAME))
            print(f"  Admin updated: {ADMIN_USERNAME}")
        else:
            execute(db, "INSERT INTO users (username, password, is_admin) VALUES (?,?,1)",
                      (ADMIN_USERNAME, hash_password(admin_pass)))
            print(f"  Admin created: {ADMIN_USERNAME}")
    print("  Database ready:", DB_FILE)

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def create_user(username, password):
    try:
        with get_db() as db:
            execute(db, "INSERT INTO users (username, password) VALUES (?,?)",
                      (username, hash_password(password)))
        return True
    except sqlite3.IntegrityError:
        return False  # username taken

def check_user(username, password):
    with get_db() as db:
        row = execute(db, "SELECT id FROM users WHERE username=? AND password=?",
                        (username, hash_password(password))).fetchone()
        return row['id'] if row else None

def user_exists(username):
    with get_db() as db:
        return fetchone(db, "SELECT id FROM users WHERE username=?", (username,)) is not None

def create_session(user_id):
    token = secrets.token_hex(32)
    with get_db() as db:
        execute(db, "DELETE FROM sessions WHERE created < ?", (time.time() - SESSION_TTL,))
        execute(db, "INSERT INTO sessions (token, user_id, created) VALUES (?,?,?)",
                  (token, user_id, time.time()))
    return token

def get_session_user(token):
    if not token: return None
    with get_db() as db:
        row = execute(db, 
            "SELECT user_id, created FROM sessions WHERE token=?", (token,)
        ).fetchone()
        if not row: return None
        if time.time() - row['created'] > SESSION_TTL:
            execute(db, "DELETE FROM sessions WHERE token=?", (token,))
            return None
        return row['user_id']

def delete_session(token):
    with get_db() as db:
        execute(db, "DELETE FROM sessions WHERE token=?", (token,))

def load_user_data(user_id):
    with get_db() as db:
        row = fetchone(db, "SELECT data FROM user_data WHERE user_id=?", (user_id,))
        if row:
            return json.loads(row['data'])
    return get_default_data()

def save_user_data(user_id, data):
    j = json.dumps(data, ensure_ascii=False)
    with get_db() as db:
        execute(db, """
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
        return fetchone(db, "SELECT COUNT(*) as n FROM users")['n']

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
                    row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                username = row['username'] if row else ''
                with get_db() as db2:
                    urow = db2.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
                    is_admin_flag = bool(urow and urow['is_admin'])
                return self.send_json(200, {'authed': True, 'username': username, 'isAdmin': is_admin_flag or username == ADMIN_USERNAME})
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
                    row = fetchone(db, "SELECT username FROM users WHERE id=?", (req_user_id,))
                    is_admin = row and (row['username'] == ADMIN_USERNAME or row.get('is_admin', 0) == 1)
            # Allow first user creation (no users yet) or admin
            if not is_admin and count_users() > 0:
                return self.send_json(403, {'error': 'רק מנהל המערכת יכול ליצור משתמשים'})
            username = payload.get('username','').strip()
            password = payload.get('password','')
            if not username or not password:
                return self.send_json(400, {'error': 'שם משתמש וסיסמה נדרשים'})
            if len(password) < 4:
                return self.send_json(400, {'error': 'סיסמה חייבת להיות לפחות 4 תווים'})
            new_user_is_admin = payload.get('is_admin', False)
            if create_user(username, password):
                # Set admin flag if requested
                if new_user_is_admin:
                    with get_db() as db:
                        execute(db, "UPDATE users SET is_admin=1 WHERE username=?", (username,))
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
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (req_user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                users = fetchall(db, "SELECT id, username, is_admin, created FROM users ORDER BY created")
                return self.send_json(200, {'users': [{'id':u['id'],'username':u['username'],'isAdmin':bool(u['is_admin']),'created':u['created']} for u in users]})

        # ── Change password (admin only) ─────────────────
        if path == '/api/change-password':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                if not row or (row['username'] != ADMIN_USERNAME and not row['is_admin']):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id = payload.get('user_id')
                new_pass  = payload.get('new_password','')
                if len(new_pass) < 4:
                    return self.send_json(400, {'error': 'סיסמה קצרה מדי'})
                execute(db, "UPDATE users SET password=? WHERE id=?", (hash_password(new_pass), target_id))
            return self.send_json(200, {'ok': True})

        # ── Set admin (admin only) ───────────────────────
        if path == '/api/set-admin':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                if not row or (row['username'] != ADMIN_USERNAME and not row['is_admin']):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id  = payload.get('user_id')
                is_admin   = payload.get('is_admin', False)
                execute(db, "UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, target_id))
            return self.send_json(200, {'ok': True})

        # ── Delete user (admin only) ──────────────────────
        if path == '/api/delete-user':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (req_user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id = payload.get('user_id')
                target = fetchone(db, "SELECT username FROM users WHERE id=?", (target_id,))
                if target and target['username'] == ADMIN_USERNAME:
                    return self.send_json(400, {'error': 'לא ניתן למחוק את מנהל המערכת'})
                execute(db, "DELETE FROM user_data WHERE user_id=?", (target_id,))
                execute(db, "DELETE FROM sessions WHERE user_id=?", (target_id,))
                execute(db, "DELETE FROM users WHERE id=?", (target_id,))
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
