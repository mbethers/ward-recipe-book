"""Postgres (Supabase) access layer for the Ward Recipe Book.

Render's free web services have no persistent disk - anything written to the
local filesystem is wiped on every redeploy/restart. So instead of SQLite, this
talks to the free Postgres database that comes with the same Supabase project
already used for photo storage (see storage.py). Use the "Transaction pooler"
connection string from Supabase (Project Settings -> Database) as DATABASE_URL
so short-lived per-request connections don't exhaust the connection limit.
"""
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from flask import g

CATEGORIES = ["Main", "Side", "Dessert", "Bread", "Breakfast", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    submitter_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Other',
    theme_tag TEXT NOT NULL DEFAULT '',
    ingredients TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    story TEXT NOT NULL DEFAULT '',
    photo_path TEXT,
    source_image_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT,
    parse_model TEXT
);

CREATE TABLE IF NOT EXISTS themes (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    reviewer_name TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

SEED_THEMES = ["Cold Cereal", "Favorite Family Recipes"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "")


def get_db() -> psycopg.Connection:
    if "db" not in g:
        database_url = _database_url()
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL isn't set. Add the Supabase Postgres connection string "
                "(Project Settings -> Database -> Connection string -> Transaction pooler)."
            )
        g.db = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Creates tables and seeds starter themes if they don't exist yet. Safe to call
    on every app startup."""
    database_url = _database_url()
    if not database_url:
        return
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("SELECT name FROM themes")
            existing = {row["name"] for row in cur.fetchall()}
            for i, theme in enumerate(SEED_THEMES):
                if theme not in existing:
                    is_current = i == len(SEED_THEMES) - 1
                    cur.execute(
                        "INSERT INTO themes (name, is_current, created_at) VALUES (%s, %s, %s)",
                        (theme, is_current, now_iso()),
                    )
        conn.commit()


def register(app):
    app.teardown_appcontext(close_db)
