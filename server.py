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
    from psycopg2 import pool as pg_pool
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
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')

# ─────────────────────────────────────────────────────────
#  DATABASE — Connection Pool
# ─────────────────────────────────────────────────────────
_pg_pool = None

def init_pool():
    global _pg_pool
    if USE_PG and DATABASE_URL and _pg_pool is None:
        try:
            _pg_pool = pg_pool.ThreadedConnectionPool(
                minconn=1, maxconn=5,
                dsn=DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            print("  ✅ PostgreSQL connection pool ready (1-5 connections)", flush=True)
        except Exception as e:
            print(f"  ⚠️ Pool init failed: {e}", flush=True)
            _pg_pool = None

class PooledConn:
    """Context manager that returns a pooled PG connection or SQLite connection."""
    def __init__(self):
        self.conn = None
        self._from_pool = False

    def __enter__(self):
        if USE_PG and DATABASE_URL:
            if _pg_pool:
                try:
                    self.conn = _pg_pool.getconn()
                    self.conn.autocommit = False
                    self._from_pool = True
                    return self.conn
                except Exception as e:
                    print(f"Pool getconn failed: {e}", flush=True)
            # Fallback: direct connection
            self.conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            return self.conn
        import sqlite3 as _sqlite3
        self.conn = _sqlite3.connect(str(DB_FILE))
        self.conn.row_factory = _sqlite3.Row
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type:
                try: self.conn.rollback()
                except: pass
            else:
                try: self.conn.commit()
                except: pass
            if self._from_pool and _pg_pool:
                try: _pg_pool.putconn(self.conn)
                except: pass
            elif not self._from_pool:
                try: self.conn.close()
                except: pass
        return False

def get_db():
    return PooledConn()

def is_pg():
    return USE_PG and bool(DATABASE_URL)

def execute_sql(db, sql):
    if is_pg():
        cur = db.cursor()
        for stmt in sql.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
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

def pg_sql(sql):
    sql = sql.replace("?", "%s")
    if is_pg():
        sql = sql.replace("datetime('now')", "NOW()")
    return sql

def fetchone(db, sql, params=()):
    if is_pg():
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql(sql), params)
        row = cur.fetchone()
        return dict(row) if row else None
    cur = db.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None

def fetchall(db, sql, params=()):
    if is_pg():
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    cur = db.execute(sql, params)
    rows = cur.fetchall()
    return [dict(r) for r in rows] if rows else []

def execute(db, sql, params=()):
    if is_pg():
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql(sql), params)
        db.commit()
        return cur
    cur = db.execute(sql, params)
    try:
        db.commit()
    except Exception:
        pass
    return cur

def init_db():
    init_pool()
    if not is_pg() and os.environ.get('RESET_DB') == '1' and DB_FILE.exists():
        DB_FILE.unlink()
        print("  Database reset!")
    with get_db() as db:
        sql = """
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            is_admin   INTEGER DEFAULT 0,
            created    TEXT DEFAULT (datetime('now')),
            last_login TEXT DEFAULT NULL
        );
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
        CREATE TABLE IF NOT EXISTS saved_schedules (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            name     TEXT NOT NULL,
            week_start TEXT,
            schedule_data TEXT NOT NULL,
            created  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS shift_notes (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            week_start TEXT,
            day      TEXT NOT NULL,
            shift    TEXT NOT NULL,
            note     TEXT NOT NULL,
            created  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS shift_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            week_start TEXT NOT NULL,
            requests   TEXT NOT NULL,
            comment    TEXT DEFAULT '',
            created    TEXT DEFAULT (datetime('now')),
            is_read    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS companies (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            plan     TEXT DEFAULT 'trial',
            active   INTEGER DEFAULT 1,
            created  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS departments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            created     TEXT DEFAULT (datetime('now'))
        );
        """
        execute_sql(db, sql)

    for col_sql in [
        "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_login TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN company_id INTEGER DEFAULT NULL",
    ]:
        try:
            with get_db() as db:
                execute(db, col_sql)
        except Exception:
            pass

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
    """Returns True on success, False if username taken."""
    try:
        with get_db() as db:
            execute(db, "INSERT INTO users (username, password) VALUES (?,?)",
                      (username, hash_password(password)))
        return True
    except Exception as e:
        # Catches both sqlite3.IntegrityError and psycopg2.IntegrityError
        err = str(e).lower()
        if 'unique' in err or 'duplicate' in err or 'integrity' in err:
            return False
        raise  # re-raise unexpected errors

def check_user(username, password):
    with get_db() as db:
        row = fetchone(db, "SELECT id FROM users WHERE username=? AND password=?",
                        (username, hash_password(password)))
        return row['id'] if row else None

def user_exists(username):
    with get_db() as db:
        return fetchone(db, "SELECT id FROM users WHERE username=?", (username,)) is not None

def create_session(user_id):
    token = secrets.token_hex(32)
    with get_db() as db:
        execute(db, "DELETE FROM sessions WHERE created < ?", (time.time() - SESSION_TTL,))
        execute(db, "INSERT INTO sessions (token, user_id, created) VALUES (?,?,?)",
                  (token, user_id, float(time.time())))
    return token

def get_session_user(token):
    if not token: return None
    try:
        with get_db() as db:
            row = fetchone(db, "SELECT user_id, created FROM sessions WHERE token=?", (token,))
            if not row: return None
            created_val = row['created']
            # Handle various types: float, str, Decimal (PostgreSQL)
            try:
                created_val = float(created_val)
            except (TypeError, ValueError):
                created_val = 0
            if time.time() - created_val > SESSION_TTL:
                execute(db, "DELETE FROM sessions WHERE token=?", (token,))
                return None
            return row['user_id']
    except Exception as e:
        print(f'get_session_user error: {e}', flush=True)
        return None

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
            ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=datetime('now')
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
        try:
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # Client disconnected — ignore

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
                    row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (user_id,))
                username = row['username'] if row else ''
                if row:
                    uname = row['username']
                    # Check role column first, fall back to is_admin
                    stored_role = row.get('role') or ''
                    if uname == ADMIN_USERNAME or stored_role == 'superadmin':
                        role = 'superadmin'
                    elif int(row.get('is_admin') or 0) == 1 or stored_role == 'manager':
                        role = 'manager'
                    else:
                        role = 'worker'
                    is_admin_flag = role in ('superadmin','manager')
                    print(f'  /api/me: {uname} -> role={role}', flush=True)
                    return self.send_json(200, {'authed':True,'username':uname,'isAdmin':is_admin_flag,'role':role,'isSuperAdmin':role=='superadmin'})
            return self.send_json(200, {'authed':False,'isAdmin':False,'role':'worker'})

        if path == '/api/data':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            return self.send_json(200, load_user_data(user_id))

        # ── Companies (GET) ───────────────────────────────
        if path == '/api/companies':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                raw = fetchall(db, "SELECT id, name, plan, active, created FROM companies ORDER BY created DESC")
                # Add user count per company
                companies_with_count = []
                for c in raw:
                    cnt = fetchone(db, "SELECT COUNT(*) as n FROM users WHERE company_id=?", (c['id'],))
                    companies_with_count.append({**c, 'user_count': cnt['n'] if cnt else 0})
                return self.send_json(200, {'companies': companies_with_count})

        # ── /api/users via GET (admin only) ──────────────
        if path == '/api/users':
            if not user_id:
                print(f'GET /api/users: no user_id from token={get_token(self)[:10] if get_token(self) else None}', flush=True)
                return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (user_id,))
                print(f'GET /api/users: user_id={user_id} row={row} ADMIN_USERNAME={ADMIN_USERNAME}', flush=True)
                is_admin_check = row and (row['username'] == ADMIN_USERNAME or int(row.get('is_admin') or 0) == 1)
                if not is_admin_check:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                sql = "SELECT u.id, u.username, u.is_admin, u.company_id, u.created, c.name as company_name FROM users u LEFT JOIN companies c ON c.id=u.company_id ORDER BY u.created"
                users = fetchall(db, sql)
                return self.send_json(200, {'users': [
                    {'id': u['id'], 'username': u['username'],
                     'isAdmin': bool(int(u.get('is_admin') or 0) == 1 or u['username'] == ADMIN_USERNAME),
                     'company_id': u.get('company_id'),
                     'company_name': u.get('company_name') or '—',
                     'created': str(u['created']) if u['created'] else ''}
                    for u in users
                ]})

        # Static files
        fp = path if path != '/' else '/index.html'
        file_path = FOLDER / fp.lstrip('/')
        try:
            file_path.resolve().relative_to(FOLDER.resolve())
        except ValueError:
            self.send_response(403); self.end_headers(); return

        if file_path.exists() and file_path.is_file():
            try:
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', MIME.get(file_path.suffix, 'application/octet-stream'))
                self.send_header('Content-Length', len(data))
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError:
                pass
        else:
            self.send_response(404)
            self.end_headers()
            try:
                self.wfile.write(b'Not found')
            except BrokenPipeError:
                pass

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
            req_user_id = get_session_user(token)
            is_admin = False
            is_super = False
            if req_user_id:
                with get_db() as db:
                    row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                    if row:
                        is_super = (row['username'] == ADMIN_USERNAME)
                        is_admin = is_super or int(row.get('is_admin') or 0) == 1
            if not is_admin and count_users() > 0:
                return self.send_json(403, {'error': 'רק מנהל יכול ליצור משתמשים'})
            username = payload.get('username','').strip()
            password = payload.get('password','')
            if not username or not password:
                return self.send_json(400, {'error': 'שם משתמש וסיסמה נדרשים'})
            if len(password) < 4:
                return self.send_json(400, {'error': 'סיסמה חייבת להיות לפחות 4 תווים'})
            new_user_is_admin = payload.get('is_admin', False)
            new_is_superadmin = payload.get('is_superadmin', False)
            # Only superadmin can grant superadmin access
            if not is_super:
                new_user_is_admin = new_user_is_admin  # keep, just not superadmin
                new_is_superadmin = False
            company_id = payload.get('company_id') or None
            new_role = 'superadmin' if (new_user_is_admin and new_is_superadmin) else ('manager' if new_user_is_admin else 'worker')
            if create_user(username, password):
                with get_db() as db:
                    if new_user_is_admin:
                        execute(db,"UPDATE users SET is_admin=1,company_id=? WHERE username=?",(company_id,username))
                    elif company_id:
                        execute(db,"UPDATE users SET company_id=? WHERE username=?",(company_id,username))
                if is_admin:
                    return self.send_json(200, {'ok': True, 'created': username})
                uid = check_user(username, password)
                t = create_session(uid)
                try:
                    body_out = json.dumps({'ok': True}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Set-Cookie', f'noc_session={t}; HttpOnly; Path=/; Max-Age={SESSION_TTL}; SameSite=Lax')
                    self.send_header('Content-Length', len(body_out))
                    self.end_headers()
                    self.wfile.write(body_out)
                except BrokenPipeError:
                    pass
            else:
                self.send_json(409, {'error': 'שם המשתמש תפוס, נסה אחר'})
            return

        # ── Login ─────────────────────────────────────────
        if path == '/api/login':
            uid = check_user(payload.get('username',''), payload.get('password',''))
            if uid:
                t = create_session(uid)
                try:
                    body_out = json.dumps({'ok': True}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Set-Cookie', f'noc_session={t}; HttpOnly; Path=/; Max-Age={SESSION_TTL}; SameSite=Lax')
                    self.send_header('Content-Length', len(body_out))
                    self.end_headers()
                    self.wfile.write(body_out)
                except BrokenPipeError:
                    pass
            else:
                self.send_json(401, {'error': 'שם משתמש או סיסמה שגויים'})
            return

        # ── List users via POST (admin only) ──────────────
        if path == '/api/users':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                if not row or (row['username'] != ADMIN_USERNAME and int(row.get('is_admin') or 0) != 1):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                users = fetchall(db, "SELECT id, username, is_admin, created FROM users ORDER BY created")
                return self.send_json(200, {'users': [
                    {'id': u['id'], 'username': u['username'],
                     'isAdmin': bool(int(u.get('is_admin') or 0) == 1 or u['username'] == ADMIN_USERNAME), 'created': u['created']}
                    for u in users
                ]})

        # ── Change password (admin only) ──────────────────
        if path == '/api/change-password':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                if not row or (row['username'] != ADMIN_USERNAME and int(row.get('is_admin') or 0) != 1):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id = payload.get('user_id')
                new_pass  = payload.get('new_password','')
                if len(new_pass) < 4:
                    return self.send_json(400, {'error': 'סיסמה קצרה מדי'})
                execute(db, "UPDATE users SET password=? WHERE id=?", (hash_password(new_pass), target_id))
            return self.send_json(200, {'ok': True})

        # ── Set admin (admin only) ────────────────────────
        if path == '/api/set-admin':
            req_user_id = get_session_user(token)
            if not req_user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (req_user_id,))
                if not row or (row['username'] != ADMIN_USERNAME and int(row.get('is_admin') or 0) != 1):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                target_id  = payload.get('user_id')
                is_admin_v = payload.get('is_admin', False)
                execute(db, "UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin_v else 0, target_id))
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
            try:
                body_out = json.dumps({'ok': True}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', 'noc_session=; HttpOnly; Path=/; Max-Age=0')
                self.send_header('Content-Length', len(body_out))
                self.end_headers()
                self.wfile.write(body_out)
            except BrokenPipeError:
                pass
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

        # ── List saved schedules ──────────────────────────
        if path == '/api/schedules/list':
            with get_db() as db:
                rows = fetchall(db, "SELECT id,name,week_start,created FROM saved_schedules WHERE user_id=? ORDER BY created DESC LIMIT 20", (user_id,))
            return self.send_json(200, {'schedules': rows})

        # ── Save named schedule ───────────────────────────
        if path == '/api/schedules/save':
            name       = payload.get('name','לוח ללא שם')
            week_start = payload.get('weekStart','')
            data       = json.dumps(payload.get('data',{}), ensure_ascii=False)
            with get_db() as db:
                execute(db, "INSERT INTO saved_schedules (user_id,name,week_start,schedule_data) VALUES (?,?,?,?)",
                       (user_id, name, week_start, data))
            return self.send_json(200, {'ok': True})

        # ── Load saved schedule ───────────────────────────
        if path == '/api/schedules/load':
            sid = payload.get('id')
            with get_db() as db:
                row = fetchone(db, "SELECT schedule_data FROM saved_schedules WHERE id=? AND user_id=?", (sid, user_id))
            if row:
                return self.send_json(200, {'data': json.loads(row['schedule_data'])})
            return self.send_json(404, {'error': 'לא נמצא'})

        # ── Delete saved schedule ─────────────────────────
        if path == '/api/schedules/delete':
            sid = payload.get('id')
            with get_db() as db:
                execute(db, "DELETE FROM saved_schedules WHERE id=? AND user_id=?", (sid, user_id))
            return self.send_json(200, {'ok': True})

        # ── Save shift note ───────────────────────────────
        if path == '/api/notes/save':
            week_start = payload.get('weekStart','')
            day   = payload.get('day','')
            shift = payload.get('shift','')
            note  = payload.get('note','')
            with get_db() as db:
                execute(db, "DELETE FROM shift_notes WHERE user_id=? AND week_start=? AND day=? AND shift=?",
                       (user_id, week_start, day, shift))
                if note.strip():
                    execute(db, "INSERT INTO shift_notes (user_id,week_start,day,shift,note) VALUES (?,?,?,?,?)",
                           (user_id, week_start, day, shift, note))
            return self.send_json(200, {'ok': True})

        # ── Load shift notes ──────────────────────────────
        if path == '/api/notes/load':
            week_start = payload.get('weekStart','')
            with get_db() as db:
                rows = fetchall(db, "SELECT day,shift,note FROM shift_notes WHERE user_id=? AND week_start=?",
                               (user_id, week_start))
            notes = {f"{r['day']}_{r['shift']}": r['note'] for r in rows}
            return self.send_json(200, {'notes': notes})

        # ── Worker: submit shift requests ──────────────────
        if path == '/api/shift-request':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row: return self.send_json(401, {'error': 'לא מחובר'})
                username = row['username']
                week_start = payload.get('weekStart', '')
                requests_data = json.dumps(payload.get('requests', {}), ensure_ascii=False)
                comment = payload.get('comment', '')
                existing = fetchone(db, "SELECT id FROM shift_requests WHERE username=? AND week_start=?", (username, week_start))
                if existing:
                    execute(db, "UPDATE shift_requests SET requests=?, comment=?, created=datetime('now'), is_read=0 WHERE username=? AND week_start=?",
                            (requests_data, comment, username, week_start))
                else:
                    execute(db, "INSERT INTO shift_requests (username, week_start, requests, comment) VALUES (?,?,?,?)",
                            (username, week_start, requests_data, comment))
            return self.send_json(200, {'ok': True})

        # ── Manager: get all shift requests for a week ────
        if path == '/api/shift-requests':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                urow = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (user_id,))
                if not urow or (urow['username'] != ADMIN_USERNAME and int(urow.get('is_admin') or 0) != 1):
                    return self.send_json(403, {'error': 'אין הרשאה'})
                week_start = payload.get('weekStart', '')
                rows = fetchall(db, "SELECT username, requests, comment, created, is_read FROM shift_requests WHERE week_start=? ORDER BY created DESC", (week_start,))
                execute(db, "UPDATE shift_requests SET is_read=1 WHERE week_start=?", (week_start,))
                return self.send_json(200, {'requests': [
                    {'username': r['username'], 'requests': json.loads(r['requests']),
                     'comment': r['comment'], 'created': str(r['created']), 'isRead': bool(r['is_read'])}
                    for r in rows
                ]})

        # ── Count unread requests ─────────────────────────
        if path == '/api/shift-requests/unread':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                urow = fetchone(db, "SELECT username, is_admin FROM users WHERE id=?", (user_id,))
                if not urow or (urow['username'] != ADMIN_USERNAME and int(urow.get('is_admin') or 0) != 1):
                    return self.send_json(200, {'count': 0})
                row = fetchone(db, "SELECT COUNT(*) as n FROM shift_requests WHERE is_read=0")
                return self.send_json(200, {'count': row['n'] if row else 0})

        # ── Worker: get MY schedule ───────────────────────
        if path == '/api/my-schedule':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                urow = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not urow: return self.send_json(401, {'error': 'לא מחובר'})
                username = urow['username']
                admin_row = fetchone(db, "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))
                if admin_row:
                    data = load_user_data(admin_row['id'])
                    return self.send_json(200, {'username': username, 'schedule': data.get('lastSchedule',{}), 'generated': data.get('lastGenerated','')})
            return self.send_json(200, {'username': '', 'schedule': {}, 'generated': ''})

        # ── Get workers list for employee dropdown ────────
        if path == '/api/workers-list':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            # Get workers from admin's data
            with get_db() as db:
                admin_row = fetchone(db, "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))
                if admin_row:
                    data = load_user_data(admin_row['id'])
                    workers = [{'name': w['name'], 'role': w.get('role','')} for w in data.get('workers', [])]
                    return self.send_json(200, {'workers': workers})
            return self.send_json(200, {'workers': []})

        # ── Companies ─────────────────────────────────────
        if path == '/api/companies':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                raw = fetchall(db, "SELECT id, name, plan, active, created FROM companies ORDER BY created DESC")
                companies_with_count = []
                for c in raw:
                    cnt = fetchone(db, "SELECT COUNT(*) as n FROM users WHERE company_id=?", (c['id'],))
                    companies_with_count.append({**c, 'user_count': cnt['n'] if cnt else 0})
                return self.send_json(200, {'companies': companies_with_count})

        if path == '/api/companies/create':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                name = payload.get('name','').strip()
                plan = payload.get('plan','trial')
                if not name: return self.send_json(400, {'error': 'שם חברה נדרש'})
                execute(db, "INSERT INTO companies (name,plan) VALUES (?,?)", (name, plan))
                comp = fetchone(db, "SELECT id FROM companies WHERE name=? ORDER BY id DESC LIMIT 1", (name,))
            return self.send_json(200, {'ok': True, 'id': comp['id'] if comp else None})

        if path == '/api/departments':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            cid = payload.get('company_id')
            if not cid: return self.send_json(400, {'error': 'company_id נדרש'})
            with get_db() as db:
                deps = fetchall(db, "SELECT id, name FROM departments WHERE company_id=? ORDER BY name", (cid,))
                return self.send_json(200, {'departments': deps})

        if path == '/api/departments/create':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                name = payload.get('name','').strip()
                cid = payload.get('company_id')
                if not name or not cid: return self.send_json(400, {'error': 'שם ו-company_id נדרשים'})
                execute(db, "INSERT INTO departments (company_id, name) VALUES (?,?)", (cid, name))
                dep = fetchone(db, "SELECT id FROM departments WHERE company_id=? AND name=? ORDER BY id DESC LIMIT 1", (cid, name))
            return self.send_json(200, {'ok': True, 'id': dep['id'] if dep else None})

        if path == '/api/admin/stats':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                import time as _time
                tc = fetchone(db, "SELECT COUNT(*) as n FROM companies")['n']
                ac = fetchone(db, "SELECT COUNT(*) as n FROM companies WHERE active=1")['n']
                tm = fetchone(db, "SELECT COUNT(*) as n FROM users WHERE is_admin=1 AND username!=?", (ADMIN_USERNAME,))['n']
                tw = fetchone(db, "SELECT COUNT(*) as n FROM users WHERE is_admin=0 AND username!=?", (ADMIN_USERNAME,))['n']
                ts = (fetchone(db, "SELECT COUNT(*) as n FROM sessions WHERE created > ?", (_time.time()-86400,)) or {}).get('n',0)
                csql = "SELECT c.id, c.name, c.plan, c.active, COUNT(DISTINCT CASE WHEN u.is_admin=1 THEN u.id END) as mgrs, COUNT(DISTINCT CASE WHEN u.is_admin=0 THEN u.id END) as wrkrs FROM companies c LEFT JOIN users u ON u.company_id=c.id GROUP BY c.id ORDER BY c.name"
                comp_breakdown = fetchall(db, csql)
                return self.send_json(200, {
                    'totalCompanies': tc, 'activeCompanies': ac,
                    'totalManagers': tm, 'totalWorkers': tw, 'totalUsers': tm+tw,
                    'activeSessions': ts,
                    'companyBreakdown': [{'id':c['id'],'name':c['name'],'plan':c['plan'],'active':c['active'],'managers':c['mgrs'],'workers':c['wrkrs']} for c in comp_breakdown]
                })

        if path == '/api/companies/update':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                cid = payload.get('id')
                name = payload.get('name','').strip()
                plan = payload.get('plan','trial')
                if not name: return self.send_json(400, {'error': 'שם נדרש'})
                execute(db, "UPDATE companies SET name=?, plan=? WHERE id=?", (name, plan, cid))
            return self.send_json(200, {'ok': True})

        if path == '/api/companies/toggle':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                cid = payload.get('company_id')
                comp = fetchone(db, "SELECT active FROM companies WHERE id=?", (cid,))
                if not comp: return self.send_json(404, {'error': 'לא נמצאה'})
                execute(db, "UPDATE companies SET active=? WHERE id=?", (0 if comp['active'] else 1, cid))
            return self.send_json(200, {'ok': True})

        if path == '/api/companies/delete':
            if not user_id: return self.send_json(401, {'error': 'לא מחובר'})
            with get_db() as db:
                row = fetchone(db, "SELECT username FROM users WHERE id=?", (user_id,))
                if not row or row['username'] != ADMIN_USERNAME:
                    return self.send_json(403, {'error': 'אין הרשאה'})
                cid = payload.get('company_id')
                execute(db, "DELETE FROM companies WHERE id=?", (cid,))
            return self.send_json(200, {'ok': True})

        if path == '/api/users/update':
            if not user_id: return self.send_json(401,{'error':'לא מחובר'})
            with get_db() as db:
                req_row = fetchone(db,"SELECT username,is_admin FROM users WHERE id=?",(user_id,))
                if not req_row or (req_row['username']!=ADMIN_USERNAME and int(req_row.get('is_admin') or 0)!=1):
                    return self.send_json(403,{'error':'אין הרשאה'})
                target_id = payload.get('user_id')
                target = fetchone(db,"SELECT username FROM users WHERE id=?",(target_id,))
                if not target: return self.send_json(404,{'error':'לא נמצא'})
                # Update username
                new_uname = payload.get('username','').strip()
                new_is_admin = 1 if payload.get('is_admin') else 0
                new_cid = payload.get('company_id')
                if new_uname:
                    execute(db,"UPDATE users SET is_admin=?,company_id=? WHERE id=?",(new_is_admin,new_cid,target_id))
                # Update password if provided
                pw = payload.get('password','')
                if pw and len(pw)>=4:
                    execute(db,"UPDATE users SET password=? WHERE id=?",(hash_password(pw),target_id))
            return self.send_json(200,{'ok':True})

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
