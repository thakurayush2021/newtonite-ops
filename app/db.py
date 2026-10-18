"""SQLite access. One connection per request, autocommit mode; writes open an explicit BEGIN IMMEDIATE."""
import os
import sqlite3
from flask import g, current_app

SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def connect(path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path):
    conn = connect(path)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
