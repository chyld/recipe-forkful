"""SQLite access: per-request connections, explicit transactions, migrations, timestamps."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class MigrationError(Exception):
    pass


def connect(path):
    """Open a connection with explicit transaction control (we issue BEGIN/COMMIT)."""
    db = sqlite3.connect(path, autocommit=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db):
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception (including a failed COMMIT)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def now():
    """The current time from the app's (injectable) clock, always timezone-aware UTC."""
    return current_app.config["CLOCK"]().astimezone(timezone.utc)


def utc_ts(dt=None):
    """The single timestamp format used for every stored value and every comparison."""
    return (dt or now()).astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(text):
    return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)


def migration_files():
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    return [(int(f.name[:4]), f) for f in files]


def migrate(db_path):
    """Bring the database up to the newest schema. Returns (old_version, [applied versions])."""
    db = connect(db_path)
    try:
        db.execute("PRAGMA journal_mode=WAL")  # persistent; must run outside a transaction
        current = db.execute("PRAGMA user_version").fetchone()[0]
        files = migration_files()
        latest = files[-1][0] if files else 0
        if current > latest:
            raise MigrationError(
                f"The database is at schema version {current}, but this version of "
                f"Forkful only knows up to {latest}. Refusing to touch it."
            )
        applied = []
        for version, path in files:
            if version <= current:
                continue
            try:
                db.executescript(path.read_text(encoding="utf-8"))
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            if db.execute("PRAGMA user_version").fetchone()[0] != version:
                raise MigrationError(f"Migration {path.name} did not set user_version to {version}.")
            applied.append(version)
        return current, applied
    finally:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
