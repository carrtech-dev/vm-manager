"""Server-side sessions and role enforcement. No browser-stored bearer tokens."""

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROLES = {
    "administrator": {"read", "operate", "users"},
    "operator": {"read", "operate"},
    "viewer": {"read"},
}


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        return hmac.compare_digest(
            hash_password(password, stored.split("$")[1]), stored
        )
    except (ValueError, IndexError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "manager.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, action TEXT, resource TEXT,
                    username TEXT, status TEXT, started REAL, finished REAL, message TEXT);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, username TEXT,
                    action TEXT, resource TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS isos(id TEXT PRIMARY KEY, name TEXT UNIQUE, path TEXT,
                    size INTEGER, sha256 TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS pending_hardware(id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    device TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(peer TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,
                    name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS tags(id TEXT PRIMARY KEY,
                    name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_projects(
                    kind TEXT NOT NULL CHECK(kind IN ('vm','network')),
                    resource_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    PRIMARY KEY(kind,resource_id));
                CREATE TABLE IF NOT EXISTS resource_tags(
                    kind TEXT NOT NULL CHECK(kind IN ('vm','network')),
                    resource_id TEXT NOT NULL,
                    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY(kind,resource_id,tag_id));
            """)
            db.execute(
                "UPDATE tasks SET status='interrupted', finished=?, message='Service restarted. Inspect the resource before retrying.' WHERE status IN ('queued','running')",
                (time.time(),),
            )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, username, action, resource="", detail="", db=None):
        if db is None:
            with self.db() as connection:
                self.audit(username, action, resource, detail, db=connection)
            return
        db.execute(
            "INSERT INTO audit(at,username,action,resource,detail) VALUES(?,?,?,?,?)",
            (time.time(), username, action, resource, detail[:2000]),
        )

    def bootstrap(self, directory: Path):
        with self.db() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return
            password = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)",
                ("admin", hash_password(password), "administrator", time.time()),
            )
            path = directory / "initial-admin.txt"
            path.write_text(
                f"Username: admin\nPassword: {password}\nChange this password in Account after signing in.\n"
            )
            path.chmod(0o600)
