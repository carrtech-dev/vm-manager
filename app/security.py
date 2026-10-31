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
                CREATE TABLE IF NOT EXISTS task_output(
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, at REAL NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY(task_id,seq));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, username TEXT,
                    action TEXT, resource TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS isos(id TEXT PRIMARY KEY, name TEXT UNIQUE, path TEXT,
                    size INTEGER, sha256 TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS pending_hardware(id TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    device TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS vm_snapshots(
                    vm_id TEXT NOT NULL, name TEXT NOT NULL, parent TEXT,
                    description TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL, operation TEXT NOT NULL,
                    manifest TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(vm_id,name));
                CREATE TABLE IF NOT EXISTS snapshot_heads(
                    vm_id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE IF NOT EXISTS attempts(peer TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS patch_guests(vm_id TEXT PRIMARY KEY, checked REAL NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS patch_schedules(id TEXT PRIMARY KEY, settings TEXT NOT NULL, next_run REAL, last_message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS patch_batches(id TEXT PRIMARY KEY, settings TEXT NOT NULL, username TEXT NOT NULL, created REAL NOT NULL, deadline REAL, schedule_id TEXT);
                CREATE TABLE IF NOT EXISTS patch_runs(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES patch_batches(id), vm_id TEXT NOT NULL,
                    status TEXT NOT NULL, task_id TEXT, attempted INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}', next_check REAL NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS patch_one_active ON patch_runs(vm_id) WHERE status IN ('pending','dispatched','running','reconnecting');
                CREATE TABLE IF NOT EXISTS storage_defaults(
                    purpose TEXT PRIMARY KEY CHECK(purpose IN ('vm','iso','backup')),
                    block_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS external_vm_refs(vm_id TEXT PRIMARY KEY);
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
            db.execute(
                "UPDATE vm_snapshots SET status='incomplete', error='Service restarted during snapshot operation. Inspect the task log and retry restore or delete the incomplete snapshot.' "
                "WHERE status IN ('creating','restoring','deleting')"
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
