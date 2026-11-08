"""User accounts and login sessions. Passwords are salted scrypt hashes; session tokens are stored only as hashes."""
import base64
import contextlib
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);"""
SESSION_SECONDS = 30 * 24 * 3600
# scrypt cost: about 16 MB and tens of milliseconds per check, slow enough to blunt guessing.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD, MAX_PASSWORD = 8, 128
# A wrong guess for an unknown email still pays the scrypt cost, so timing does not reveal which emails exist.
DUMMY_HASH = None


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    encode = lambda raw: base64.b64encode(raw).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def check_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email):
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL.match(email):
        raise ValueError("邮箱格式不正确")
    return email


class LoginThrottle:
    """Too many wrong passwords for one email in a window blocks further tries for that email."""

    def __init__(self, limit=10, window=15 * 60):
        self.limit, self.window, self.failures, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, email):
        with self.lock:
            recent = [moment for moment in self.failures.get(email, []) if time.time() - moment < self.window]
            self.failures[email] = recent
            return len(recent) >= self.limit

    def failed(self, email):
        with self.lock:
            self.failures.setdefault(email, []).append(time.time())

    def succeeded(self, email):
        with self.lock:
            self.failures.pop(email, None)


class AccountStore:
    def __init__(self, path):
        self.path = Path(path)
        self.throttle = LoginThrottle()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA)
        return connection

    @staticmethod
    def _user(row):
        return {"id": row[0], "email": row[1], "is_admin": bool(row[2])} if row else None

    def register(self, email, password, is_admin=False):
        email = normalize_email(email)
        if not MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD:
            raise ValueError(f"密码需要 {MIN_PASSWORD} 到 {MAX_PASSWORD} 位")
        try:
            with contextlib.closing(self.connect()) as connection, connection:
                user_id = connection.execute("INSERT INTO users (email, password, is_admin, created_at) VALUES (?, ?, ?, ?)",
                                             (email, hash_password(password), int(is_admin), time.strftime("%Y-%m-%dT%H:%M:%S%z"))).lastrowid
        except sqlite3.IntegrityError as error:
            raise ValueError("这个邮箱已经注册过") from error
        return {"id": user_id, "email": email, "is_admin": is_admin}

    def authenticate(self, email, password):
        """Return the user, None for a wrong email or password, or raise PermissionError when throttled."""
        global DUMMY_HASH
        try:
            email = normalize_email(email)
        except ValueError:
            return None
        if self.throttle.blocked(email):
            raise PermissionError("尝试次数过多，请 15 分钟后再试")
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT id, email, is_admin, password FROM users WHERE email = ? AND disabled = 0", (email,)).fetchone()
        if row is None:
            DUMMY_HASH = DUMMY_HASH or hash_password(secrets.token_hex(8))
            check_password(password or "", DUMMY_HASH)
            self.throttle.failed(email)
            return None
        if not check_password(password or "", row[3]):
            self.throttle.failed(email)
            return None
        self.throttle.succeeded(email)
        return self._user(row)

    def start_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                               (token_hash(token), user_id, now, now + SESSION_SECONDS))
        return token

    def user_for_token(self, token):
        if not token or len(token) > 200:
            return None
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT u.id, u.email, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", (token_hash(token), time.time())).fetchone()
        return self._user(row)

    def end_session(self, token):
        if token:
            with contextlib.closing(self.connect()) as connection, connection:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def find(self, email):
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT id, email, is_admin FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
        return self._user(row)
