import os
import sqlite3
from pathlib import Path
from typing import Generator, Optional

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG

DEFAULT_DB_PATH = str(DEFAULT_DATABASE_CONFIG.database_path)


def enable_wal_mode(db_path: str) -> None:
    """Permanently set WAL journal mode on the database file."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    finally:
        conn.close()


def get_write_connection(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """Create a writable SQLite connection optimized for bulk ETL ingestion."""
    # Ensure directory exists
    Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_readonly_connection(
    db_path: str,
    mmap_size: int = 2147483648,
    cache_size: int = -64000,
) -> sqlite3.Connection:
    """Create an optimized read-only SQLite connection for concurrent queries."""
    abs_path = Path(db_path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"Database file not found: {abs_path}")

    # Use URI mode=ro to bypass write-lock overhead and guarantee read concurrency
    db_uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1;")
    conn.execute(f"PRAGMA mmap_size = {mmap_size};")
    conn.execute(f"PRAGMA cache_size = {cache_size};")
    return conn


def get_db(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency yielding a thread-safe read-only connection."""
    target_path = db_path or os.environ.get("ONTOLOGY_DB_PATH", DEFAULT_DB_PATH)
    conn = get_readonly_connection(target_path)
    try:
        yield conn
    finally:
        conn.close()
