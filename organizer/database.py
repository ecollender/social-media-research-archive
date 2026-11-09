"""Deterministic SQLite lifetime: commit/rollback AND close, on every path."""
import sqlite3
from contextlib import contextmanager

@contextmanager
def connection(path,timeout=30):
    conn=sqlite3.connect(path,timeout=timeout)
    conn.row_factory=sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()
