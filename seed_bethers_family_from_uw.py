"""One-time extraction of Jenae's UW Cookbook recipes into a portable seed
file for the not-yet-created Bethers Family Cookbook database. Mirrors the
d1_import_output.json pattern: fully prepared, ready to load once a real
database exists, nothing written to any database yet.

Local admin tool - not part of the deployed app, not in requirements.txt.
"""
import json
import os
from datetime import datetime, timezone

import psycopg
import requests
from psycopg.rows import dict_row

OUTPUT_PATH = "bethers_family_seed.json"
STAGED_PHOTOS_DIR = "bethers_family_seed_photos"

PROVENANCE_NOTE = "\n\n(Originally shared in the University Ward Cookbook.)"


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL not set")

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT * FROM recipes WHERE submitter_name ILIKE %s AND status = 'published' "
            "ORDER BY id",
            ("%jenae%",),
        ).fetchall()

    os.makedirs(STAGED_PHOTOS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    seed = []
    for r in rows:
        staged_photos = []
        if r["source_image_path"]:
            for i, url in enumerate(p.strip() for p in r["source_image_path"].split("\n") if p.strip()):
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                ext = os.path.splitext(url)[1] or ".jpg"
                local_name = f"uw{r['id']}_{i}{ext}"
                local_path = os.path.join(STAGED_PHOTOS_DIR, local_name)
                with open(local_path, "wb") as fh:
                    fh.write(resp.content)
                staged_photos.append(local_path)
                print(f"  staged {local_path} ({len(resp.content)} bytes) from {url}")

        seed.append({
            "uw_recipe_id": r["id"],  # provenance only, not a schema field
            "name": r["name"],
            "submitter_name": r["submitter_name"],
            "category": r["category"],
            "cuisine": r["cuisine"],
            "dietary_tags": r["dietary_tags"],
            "prep_time": r["prep_time"],
            "servings": r["servings"],
            "ingredients": r["ingredients"],
            "instructions": r["instructions"],
            "story": (r["story"] or "") + PROVENANCE_NOTE,
            "photo_path": None,
            "source_image_path": "",  # cleared - staged_photos below is the
                                       # real record; re-upload into the new
                                       # cookbook's own storage at real-import
                                       # time rather than keep a live
                                       # dependency on UW's Supabase project
            "staged_photos": staged_photos,  # local paths, not a DB column -
                                              # the real import step uploads
                                              # these and fills photo_path
            "source_url": r["source_url"],
            "status": "published",  # already-vetted UW content, same
                                     # reasoning as the D1 import
            "submitted_at": now,
            "reviewed_at": None,
            "parse_model": "",
            "proofreading_notes": "",
        })
        print(f"#{r['id']} {r['name']!r} -> prepared")

    with open(OUTPUT_PATH, "w") as fh:
        json.dump(seed, fh, indent=2, ensure_ascii=False)

    print(f"\n{len(seed)} recipe(s) written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
