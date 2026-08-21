"""Loads bethers_family_seed.json (built by seed_bethers_family_from_uw.py)
into the shared cookbook database, tagged cookbook='family'. Local admin
tool - NOT part of the deployed Flask app, not in requirements.txt.

The Bethers Family cookbook is one of three cookbooks sharing one database
(see cookbooks.py), not its own separate database - mirrors
bulk_import_pdf.py's --commit/--reset-first shape for that reason: any
reset here is scoped to cookbook='family' only, never a bare TRUNCATE.

Dry-run (default, no DATABASE_URL read, no photos uploaded):
    ./venv/bin/python3 seed_bethers_family_commit.py

Real run, against the shared production database (also needs SUPABASE_URL /
SUPABASE_SERVICE_KEY set, to re-upload each recipe's staged photos into this
cookbook's own storage folder rather than keep a dependency on UW's):
    DATABASE_URL=postgresql://... SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \\
        ./venv/bin/python3 seed_bethers_family_commit.py --commit [--reset-first]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as db_module  # noqa: E402
import storage  # noqa: E402

SEED_PATH = "bethers_family_seed.json"

# The real schema columns a row may be inserted with - the seed JSON also
# carries uw_recipe_id (provenance only) and staged_photos (local file
# paths, not a DB column), which must never reach the INSERT's column list.
INSERT_COLUMNS = (
    "name", "cookbook", "submitter_name", "category", "cuisine", "dietary_tags",
    "prep_time", "servings", "ingredients", "instructions", "story",
    "photo_path", "source_image_path", "source_url", "status",
    "submitted_at", "reviewed_at", "parse_model", "proofreading_notes",
)


def _upload_staged_photos(staged_photos: list) -> str:
    """Re-uploads each locally-staged photo into this cookbook's own storage
    folder and returns them newline-joined, matching the multi-photo
    source_image_path convention submit_photo() already uses."""
    urls = []
    for local_path in staged_photos:
        with open(local_path, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(local_path)[1].lower() or ".jpg"
        content_type = "image/png" if ext == ".png" else "image/jpeg"
        url = storage.upload_bytes(data, f"source{ext}", content_type, "family/sources")
        urls.append(url)
        print(f"  uploaded {local_path} -> {url}")
    return "\n".join(urls)


def build_row(entry: dict, upload_photos: bool) -> dict:
    row = {col: entry.get(col, "") for col in INSERT_COLUMNS}
    row["cookbook"] = "family"
    row["reviewed_at"] = None  # JSON stores null; keep it a real NULL, not "null"

    staged_photos = entry.get("staged_photos") or []
    if staged_photos and upload_photos:
        row["source_image_path"] = _upload_staged_photos(staged_photos)
    elif staged_photos:
        row["source_image_path"] = f"<{len(staged_photos)} photo(s) staged, not uploaded - dry-run>"

    return row


def run_dry(entries: list) -> None:
    print(f"{len(entries)} recipe(s) in {SEED_PATH}:\n")
    for entry in entries:
        row = build_row(entry, upload_photos=False)
        photo_note = row["source_image_path"] or "(no photos)"
        print(f"  - {row['name']!r} by {row['submitter_name']!r} - {photo_note}")
    print("\nDry-run only - nothing was written. Use --commit to load for real.")


def run_commit(database_url: str, entries: list, reset_first: bool) -> None:
    import psycopg
    from psycopg.rows import dict_row

    if not storage.storage_configured():
        sys.exit(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY aren't set - can't re-upload staged "
            "photos. Set them (same values used for the live app) and try again."
        )

    # Idempotent migration chain (includes the cookbook column) - safe to run
    # against a database that already holds other cookbooks' data.
    os.environ["DATABASE_URL"] = database_url
    db_module.init_db()

    rows = [build_row(entry, upload_photos=True) for entry in entries]

    # prepare_threshold=None: Supabase's pooler runs PgBouncer in transaction
    # mode, which doesn't support psycopg3's default server-side prepared
    # statements (a name collision across pooled connections can raise
    # DuplicatePreparedStatement, especially with executemany's batch path).
    with psycopg.connect(database_url, row_factory=dict_row, prepare_threshold=None) as conn:
        if reset_first:
            deleted = conn.execute("DELETE FROM recipes WHERE cookbook = 'family'")
            conn.commit()
            print(f"Deleted {deleted.rowcount} existing Bethers Family recipe(s).")

        placeholders = ", ".join(f"%({c})s" for c in INSERT_COLUMNS)
        sql = f"INSERT INTO recipes ({', '.join(INSERT_COLUMNS)}) VALUES ({placeholders})"
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
        print(f"Inserted {len(rows)} recipe(s) into {database_url.split('@')[-1]} (cookbook='family').")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=SEED_PATH)
    parser.add_argument("--commit", action="store_true", help="write to DATABASE_URL for real")
    parser.add_argument(
        "--reset-first", action="store_true",
        help="delete the Bethers Family cookbook's existing recipes (cookbook='family' only) before inserting",
    )
    args = parser.parse_args()

    if not os.path.exists(args.seed):
        sys.exit(f"No {args.seed} found - run seed_bethers_family_from_uw.py first.")
    with open(args.seed) as fh:
        entries = json.load(fh)
    if not entries:
        sys.exit(f"{args.seed} has no recipes - nothing to commit.")

    if args.commit:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            sys.exit("--commit requires DATABASE_URL to be set.")
        run_commit(database_url, entries, args.reset_first)
    else:
        run_dry(entries)


if __name__ == "__main__":
    main()
