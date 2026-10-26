"""SQLite: one file, no server. Connections are opened per operation (cheap) so threads never share one.

Migrations are numbered .sql files applied once, in order, and recorded in schema_version. Each file runs in one
transaction together with its schema_version row: a migration that fails halfway leaves nothing behind.
"""
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH

log = logging.getLogger("sidekick.db")
MIGRATIONS = Path(__file__).parent / "migrations"
_path: Path = Path(DB_PATH)


def init(path: str | Path | None = None) -> Path:
    """Point at a database file (tests use a temp file) and bring its schema up to date."""
    global _path
    if path is not None:
        _path = Path(path)
    _path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT "
                     "NOT NULL DEFAULT (datetime('now')))")
        done = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
        for file in sorted(MIGRATIONS.glob("*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version not in done:  # executescript doesn't open a transaction itself: BEGIN...COMMIT does
                conn.executescript(f"BEGIN;\n{file.read_text()}\n;"
                                   f"INSERT INTO schema_version (version) VALUES ({version});\nCOMMIT;")
                log.info("Applied migration %s", file.name)
    return _path


@contextmanager
def connect():
    """One short-lived connection; commits on success, rolls back on error."""
    conn = sqlite3.connect(_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
