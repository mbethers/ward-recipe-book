"""Postgres (Supabase) access layer for the Ward Cookbook.

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

CATEGORIES = ["Appetizer", "Soup", "Salad", "Main", "Side", "Dessert", "Bread", "Breakfast", "Other"]

CUISINES = [
    "American", "Mexican", "Italian", "Indian", "Chinese", "Thai", "Japanese",
    "Korean", "Vietnamese", "Filipino", "Chilean", "Brazilian", "Peruvian",
    "Greek", "French", "German", "Mediterranean", "Hawaiian",
    "African", "Other",
]

# Not mutually exclusive - stored as a comma-separated list on the recipe
# (e.g. "Vegan,Gluten-free"), unlike category/cuisine which are single-select.
DIETARY_TAGS = ["Vegan", "Vegetarian", "Gluten-free", "Dairy-free"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    submitter_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Other',
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

CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    reviewer_name TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dish_photos (
    id SERIAL PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    uploader_name TEXT NOT NULL DEFAULT '',
    photo_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Corrections the public suggests on an already-published recipe. Kept in their
-- own table rather than as extra rows in `recipes` so that publicly-supplied
-- text is never one missing "WHERE status = 'published'" away from the live
-- site. `proposed` holds ONLY the fields that actually differ, so approving an
-- old suggestion can't quietly revert a field the admin edited since; `base`
-- holds the live values of those same keys at suggest time, purely so the
-- review screen can flag "this recipe changed since this was suggested".
-- status is pending/approved/rejected - deliberately not dish_photos'
-- 'published', which is the wrong word for an edit.
CREATE TABLE IF NOT EXISTS recipe_edits (
    id SERIAL PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    suggester_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    proposed TEXT NOT NULL DEFAULT '{}',
    base TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT
);
"""

# One-off cleanup for themes, since CREATE TABLE IF NOT EXISTS above won't
# touch a table/column that already exists. Safe to run on every startup -
# both are no-ops once applied.
DROP_THEMES = """
ALTER TABLE recipes DROP COLUMN IF EXISTS theme_tag;
DROP TABLE IF EXISTS themes;
"""

# Adds cuisine (single-select, like category) and dietary_tags (comma-separated,
# multi-select) to existing databases. Safe to run on every startup.
ADD_CUISINE_AND_DIETARY = """
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS cuisine TEXT NOT NULL DEFAULT '';
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS dietary_tags TEXT NOT NULL DEFAULT '';
"""

# Stores the original webpage URL for link-imported recipes (so admins can
# click through to verify, same idea as source_image_path for photo/PDF
# uploads). Safe to run on every startup.
ADD_SOURCE_URL = """
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT '';
"""

# Newline-separated list of possible typos/wording issues Claude flagged at
# submission time (see ai_parse.proofread_recipe) - shown to the admin during
# review, never auto-applied. Safe to run on every startup.
ADD_PROOFREADING_NOTES = """
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS proofreading_notes TEXT NOT NULL DEFAULT '';
"""

# Free-text so submitters/admins aren't boxed into a number format ("30 min",
# "1 hr 15 min", "8-10 servings", "makes 2 dozen"). Safe to run on every startup.
ADD_PREP_TIME_AND_SERVINGS = """
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS prep_time TEXT NOT NULL DEFAULT '';
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS servings TEXT NOT NULL DEFAULT '';
"""

# Multi-tenant migration: this database now serves three cookbooks (see
# cookbooks.py) instead of one. Every table that holds tenant data gets a
# `cookbook` column, defaulted to 'uw' so every row that already exists
# (this app's live production data) is correctly backfilled as University
# Ward's without a separate UPDATE - a constant DEFAULT on ADD COLUMN is a
# metadata-only operation on Postgres 11+, safe and non-blocking on a live
# table. Indexes match the WHERE clauses every query now needs. Safe to run
# on every startup, like every other block here.
ADD_COOKBOOK_COLUMN = """
ALTER TABLE recipes      ADD COLUMN IF NOT EXISTS cookbook TEXT NOT NULL DEFAULT 'uw';
ALTER TABLE reviews      ADD COLUMN IF NOT EXISTS cookbook TEXT NOT NULL DEFAULT 'uw';
ALTER TABLE dish_photos  ADD COLUMN IF NOT EXISTS cookbook TEXT NOT NULL DEFAULT 'uw';
ALTER TABLE recipe_edits ADD COLUMN IF NOT EXISTS cookbook TEXT NOT NULL DEFAULT 'uw';

CREATE INDEX IF NOT EXISTS idx_recipes_cookbook        ON recipes(cookbook);
CREATE INDEX IF NOT EXISTS idx_recipes_cookbook_status  ON recipes(cookbook, status);
CREATE INDEX IF NOT EXISTS idx_reviews_cookbook         ON reviews(cookbook);
CREATE INDEX IF NOT EXISTS idx_dish_photos_cookbook     ON dish_photos(cookbook);
CREATE INDEX IF NOT EXISTS idx_recipe_edits_cookbook    ON recipe_edits(cookbook);
"""

# settings' primary key was just `key` (one global row per setting, e.g. the
# homepage welcome message) - each cookbook now wants its own value for the
# same key, so the key alone can no longer be unique. Adding the column is
# the same safe pattern as above; swapping the primary key needs its own
# idempotent guard since Postgres has no "ADD CONSTRAINT IF NOT EXISTS" -
# this checks pg_constraint directly so it's a no-op on every run after the
# first.
ADD_COOKBOOK_TO_SETTINGS = """
ALTER TABLE settings ADD COLUMN IF NOT EXISTS cookbook TEXT NOT NULL DEFAULT 'uw';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'settings_pkey' AND conrelid = 'settings'::regclass
    ) THEN
        ALTER TABLE settings DROP CONSTRAINT settings_pkey;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'settings_cookbook_key_pkey' AND conrelid = 'settings'::regclass
    ) THEN
        ALTER TABLE settings ADD CONSTRAINT settings_cookbook_key_pkey PRIMARY KEY (cookbook, key);
    END IF;
END $$;
"""

DEFAULT_INTRO_TEXT = (
    "Welcome to the University (Married Student) Ward's Cookbook. We invite you "
    "to add your own favorite recipes, then make & comment on the recipes of "
    "others."
)


def get_setting(cookbook: str, key: str, default: str = "") -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE cookbook = %s AND key = %s", (cookbook, key)
    ).fetchone()
    return row["value"] if row else default


def set_setting(cookbook: str, key: str, value: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO settings (cookbook, key, value) VALUES (%s, %s, %s)
           ON CONFLICT (cookbook, key) DO UPDATE SET value = EXCLUDED.value""",
        (cookbook, key, value),
    )
    conn.commit()


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
    """Creates tables and seeds defaults if they don't exist yet. Safe to call
    on every app startup."""
    database_url = _database_url()
    if not database_url:
        return
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute(DROP_THEMES)
            cur.execute(ADD_CUISINE_AND_DIETARY)
            cur.execute(ADD_SOURCE_URL)
            cur.execute(ADD_PROOFREADING_NOTES)
            cur.execute(ADD_PREP_TIME_AND_SERVINGS)
            cur.execute(ADD_COOKBOOK_COLUMN)
            cur.execute(ADD_COOKBOOK_TO_SETTINGS)
            # Only UW gets this specific seeded welcome message - it names
            # this ward by name, so it isn't a sensible default for D1 or
            # the Bethers Family. Those cookbooks simply have no intro_text
            # row until an admin sets one via /admin/settings, which
            # get_setting's `default` parameter already handles gracefully.
            cur.execute(
                """INSERT INTO settings (cookbook, key, value) VALUES ('uw', 'intro_text', %s)
                   ON CONFLICT (cookbook, key) DO NOTHING""",
                (DEFAULT_INTRO_TEXT,),
            )
        conn.commit()


def register(app):
    app.teardown_appcontext(close_db)
