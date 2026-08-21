"""One-time bulk import of the Durham 1st Ward Cookbook PDF into the shared
cookbook database (D1 is one of three cookbooks sharing one database - see
cookbooks.py - not its own separate database). Local admin tool - NOT part
of the deployed Flask app, not in requirements.txt. Needs `pip install
pypdf` in the local venv.

Dry-run (default, no DATABASE_URL read at all):
    ./venv/bin/python3 bulk_import_pdf.py

Real run, against the shared production database:
    DATABASE_URL=postgresql://... ./venv/bin/python3 bulk_import_pdf.py --commit [--reset-first]

Every inserted row is tagged cookbook='d1', and --reset-first only ever
deletes cookbook='d1' rows - safe to run against a database that already
holds UW's and the Bethers Family's data.

Everything is resumable per-section: a completed section's recipes are
written to the output JSON as soon as that section succeeds, so a failure on
section 5 of 7 doesn't lose sections 1-4 or force re-spending API calls on them.
"""
import argparse
import base64
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import anthropic
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import CATEGORIES, CUISINES  # noqa: E402
from style_transforms import transform  # noqa: E402

PDF_PATH = os.path.expanduser("~/Downloads/Durham 1st Ward Cookbook.pdf")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d1_import_output.json")

# The cookbook's own cover page: "Recipes compiled May 2024." Used as
# submitted_at so the site shows when these were actually written, not
# whatever day this script happens to run.
D1_COMPILE_DATE = "2024-05-01T00:00:00+00:00"

MODEL = "claude-sonnet-5"  # one-time, bounded cost; fidelity over latency for
                            # irreplaceable historical content
MAX_TOKENS = 8192          # broadly-supported ceiling; truncation is handled by
                            # detecting stop_reason and bisecting, not by trying
                            # to guess a number big enough for every section

# 1-based, inclusive page numbers - all directly confirmed by reading the PDF,
# not inferred from the table of contents.
SECTIONS = [
    {"name": "Breakfast", "start": 3, "end": 5, "category": "Breakfast", "expected": 6},
    {"name": "Soups", "start": 6, "end": 12, "category": "Soup", "expected": 13},
    {"name": "Salads & Sides", "start": 13, "end": 17, "category": None, "expected": 10},
    {"name": "Main Dishes", "start": 18, "end": 35, "category": "Main", "expected": 33},
    {"name": "Breads & Rolls", "start": 36, "end": 40, "category": "Bread", "expected": 5},
    {"name": "Desserts", "start": 41, "end": 63, "category": "Dessert", "expected": 39},
    {"name": "Appetizers/Snacks", "start": 64, "end": 68, "category": "Appetizer", "expected": 12},
]

# Salads & Sides doesn't map 1:1 onto a single category - classified by hand
# this session, matched against the extracted name (case/whitespace-insensitive).
SALADS_SIDES_CATEGORY = {
    "confetti coleslaw": "Salad",
    "corn souffle": "Side",
    "cornucopia salad": "Salad",
    "creamy confetti corn with bacon": "Side",
    "creamy potato salad": "Side",
    "frog-eyed salad": "Salad",
    "loaded fresh basil and sweet corn couscous green salad": "Salad",
    "raspberry pretzel salad": "Salad",
    "sweet potato souffle": "Side",
    "tex-mex salad": "Salad",
}

# Known duplicate-name recipes, confirmed by directly reading their printed
# page headers (not just the table of contents, which adds "- Elise"/"-
# Breanna" disambiguation suffixes that are NOT part of the printed title).
EXPECTED_NAME_COUNTS = {
    "white chicken chili": 2,               # Elise Shipley, Sili Lautaha
    "white chicken chili (best ever)": 1,   # Elizabeth Christensen and Kaylene Myers
    "cowboy caviar": 2,                     # Breanna Gibby, Kaylene Myers
}

# The exact printed page title for every recipe, transcribed by hand from the
# table of contents. Used for a per-name diff against the extracted output -
# this is what actually caught the recipes below, not the aggregate count
# check, which a section-internal bisection cut can silently pass by
# coincidence (a recipe that vanishes with no trace, netted against an
# unrelated orphaned fragment elsewhere in the same section, still sums to
# the right total). A handful of these TOC labels differ slightly from the
# recipe's own on-page title (e.g. "Korean Beef and Rice Recipe" in the TOC
# vs "Korean Beef and Rice" on the page, or the TOC's "-Elise"/"-Breanna"
# submitter suffixes, which aren't part of the printed title at all) - those
# are benign source-document quirks, not extraction bugs, and get folded in
# as accepted aliases below rather than flagged every run.
TOC_NAMES = {
    "Breakfast": ["Holiday Syrup", "Independence Day Pancakes",
                  "Lemon Ricotta Pancakes with Blueberry Compote", "Light Waffles",
                  "Magleby's Fresh Amazing Syrup", "Make-Ahead Freezer Breakfast Sandwiches"],
    "Soups": ["Baked Potato Soup", "Creamy Chicken and Wild Rice Soup", "Hamburger Chili",
              "Jason's Hearty Chili", "Rustic Italian Tortellini Soup",
              "Slow Cooker Tortellini Sausage Potato Soup", "Standridge Lentil Soup",
              "Sunday Night Stew", "Thai Red Curry Noodle Soup", "Tomato Tortellini Soup",
              "White Chicken Chili", "White Chicken Chili", "White Chicken Chili (Best Ever)"],
    "Salads & Sides": ["Confetti Coleslaw", "Corn Souffle", "Cornucopia Salad",
                        "Creamy Confetti Corn with Bacon", "Creamy Potato Salad", "Frog-Eyed Salad",
                        "Loaded Fresh Basil and Sweet Corn Couscous Green Salad",
                        "Raspberry Pretzel Salad", "Sweet Potato Souffle", "Tex-Mex Salad"],
    "Main Dishes": ["Butternut Squash Pasta Sauce", "Campbell's Ultimate Chicken Pot Pie",
                     "Chicago Style Pizza", "Chicken Cobbler", "Chicken Stroganoff",
                     "Creamy Cajun Shrimp Pasta", "Creamy Corn Pasta with Basil",
                     "Creamy Pappardelle with Chicken and Bacon", "Creamy Tuscan Shrimp (Keto Friendly)",
                     "Crockpot Mexican Chicken", "Easy Spaghetti Bolognese",
                     "Easy Stove-Top Macaroni & Cheese", "Fiesta Chicken", "Ham and Cheese Sliders",
                     "Hawaiian Grilled Chicken Marinade and Grilled Pineapple",
                     "Honey Lime Chicken Enchiladas", "Instant Pot Chicken Pot Pie",
                     "Instant Pot Creamy \"Baked\" Ziti", "Italian Spaghetti", "Joseph's Easy Enchiladas",
                     "Korean Beef and Rice", "Lemon Fusilli with Arugula",
                     "Monterrey Chicken with Pico de Gallo {Chili's Copycat}", "Parmesan Chicken Bake",
                     "Parmesan Crusted Tilapia", "Slow Cooker Pumpkin, Chickpea & Red Lentil Curry",
                     "Spaghetti and Meatballs", "Standridge Chicken Legs",
                     "Sweet Potato Apple Sausage Sheet Pan",
                     "Thai Lettuce Wraps with Peanut Sauce (The Cheesecake Factory)",
                     "True Carolina Shrimp Boil", "Tofu Power Bowls", "Zesty Italian Chicken"],
    "Breads & Rolls": ["Bread Recipe", "Overnight Sourdough Recipe", "Sage's Perfect Potato Roll",
                        "Salted French Bread", "Variations on the Southern Biscuit"],
    "Desserts": ["Apple Dumplings", "Banana Blondies", "Banana Muffins",
                 "Berry Galette (Buttermilk Cornmeal Crust)", "Best Homemade Brownies",
                 "Best Oatmeal Raisin Cookies Ever", "Buttermilk Sheet Cake", "Caramel Brownies",
                 "Chocolate Chess Pie", "Chocolate Trifle", "Cinnamon Crunch Banana Bread",
                 "Cinnamon Roll Cake", "Cinnamon Rolls {Made with Sourdough Starter!}",
                 "Coconut Cupcakes", "Foster's Market Peanut Butter Pie", "Fruit Cobbler",
                 "Gluten-Free Chocolate Cupcakes", "Grand Canyon Coffee Cake", "Honey Crunch Pecan Pie",
                 "Hot Fudge Cake", "Katelyn Hall's Chocolate Chip Cookies", "Mini Pavlova",
                 "Monster Cookies", "Mrs. Sigg's Snickerdoodles", "No Cook Chocolate Oatmeal Cookies",
                 "Nordstrom Café White Chocolate Bread Pudding", "Peanut Butter Cup Pie",
                 "Perfect Chocolate Chip Cookies", "Pete's Peanut Butter Pinwheels", "Pumpkin Bread",
                 "Pumpkin Muffins", "Pumpkin Muffins", "Russian Tea Cakes", "Sourdough Brownies",
                 "Strawberry Shortcake", "Sugar Cookie and Royal Frosting", "Symphony Bars",
                 "The Best Soft Chocolate Chip Cookies", "Toffee Ice Cream Pie"],
    "Appetizers/Snacks": ["Apple \"Pizza\" Snacks", "Chicken Puffs", "Cowboy Caviar", "Cowboy Caviar",
                           "Easy White Chicken Chili Corn Dip", "Ham and Cheese Spinach Puffs",
                           "Protein Shake", "Seven Wives Inn Granola", "Spicy Black Bean and Corn Salsa",
                           "Spinach Artichoke Dip", "Spinach Cheese Bake", "Tomatillo Salsa"],
}

EXTRACTION_SYSTEM_PROMPT = f"""You are transcribing every recipe on these pages of a \
church ward's printed cookbook, for a searchable online version. Each recipe has a bold \
title, a submitter's name (usually to the right of the title), sometimes a short intro \
line or personal story, then a two-column ingredient list, then instructions.

Return ONLY a valid JSON array, no preamble, no markdown code fences, no commentary. One \
object per COMPLETE recipe found on these pages - extract every single one, do not \
summarize, skip, or omit any, even if there are many. Each object has exactly this shape:

{{
  "name": "recipe title exactly as printed",
  "submitter_name": "exactly as printed - preserve multi-author credits ('Elizabeth \
Christensen and Kaylene Myers') and 'via' chains ('Kitty Bennet via Yoalder Meyer') verbatim",
  "cuisine": "one of {", ".join(CUISINES)} - your best guess from the ingredients/style, \
or empty string if you genuinely can't tell",
  "prep_time": "total time, only if actually stated on the page, else empty string",
  "servings": "servings/yield, only if actually stated on the page, else empty string",
  "ingredients": ["ingredient line 1", "ingredient line 2", ...],
  "instructions": ["step 1", "step 2", ...],
  "story": "any intro line, memory, or 'Adapted from X, another ward member' note - or empty string"
}}

Rules:
- Do not invent, guess, or reword ANY ingredient or instruction that isn't on the page.
- If a recipe has labeled sub-sections (e.g. "Dough:", "Sauce:", "Toppings:", "Starter/Feeder:",
  "Materials:", or named variations like "Basic Biscuit" / "Sour Cream Biscuit" under one
  title), keep each label as its own array entry ending in a colon (or as its own bold
  sub-title), in its original position, exactly as it appears - do not merge sections or
  reorder anything.
- Keep each ingredient as its own array entry, not one big blob. Same for instructions.
- Do NOT include a "category" field - category is assigned separately, not by you.
- Do not guess at prep_time or servings if they aren't actually written on the page.
- If this batch of pages has zero recipes on them (e.g. only a section divider), return [].
"""


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY isn't set.")
    # No gunicorn worker timeout applies here - this is a local script, not the
    # live web app - so a long timeout is safe and appropriate for a call that
    # may generate a large JSON array.
    return anthropic.Anthropic(api_key=api_key, timeout=300.0, max_retries=2)


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def split_pdf_pages(reader: PdfReader, start_printed: int, end_printed: int) -> bytes:
    """Inclusive range of PRINTED (footer) page numbers -> bytes of a new PDF
    containing just those pages.

    The PDF's unnumbered cover page (pypdf index 0) sits before the printed
    numbering starts, so pypdf's 0-based page index equals the printed page
    number directly - printed page "3" is reader.pages[3], not reader.pages[2].
    Verified empirically (not assumed) before this shipped: reader.pages[3]'s
    extracted text starts with the "3 Breakfast Holiday Syrup..." footer/header,
    confirming the offset. Do not "simplify" this back to start_printed - 1.
    """
    writer = PdfWriter()
    for i in range(start_printed, end_printed + 1):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _slice_pdf_0based(reader: PdfReader, start_0based: int, end_0based_inclusive: int) -> bytes:
    """Plain 0-based page slice of a STANDALONE PdfReader - i.e. one already
    written by PdfWriter, with no unnumbered cover page in front of it, so
    index 0 really is that reader's own first page. Used only to re-split an
    already-extracted section chunk during bisection - NOT the same thing as
    split_pdf_pages(), which is calibrated for the original 69-page document's
    cover-page offset. Mixing the two up reintroduces an off-by-one."""
    writer = PdfWriter()
    for i in range(start_0based, end_0based_inclusive + 1):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def preview_chunk(reader: PdfReader, start_printed: int, end_printed: int) -> None:
    """Human sanity check before spending an API call: page count + a text
    snippet, so a page-range mistake is caught before it costs anything."""
    n_pages = end_printed - start_printed + 1
    first_page_text = reader.pages[start_printed].extract_text() or ""
    snippet = " ".join(first_page_text.split())[:200]
    print(f"    {n_pages} page(s), printed page {start_printed} starts: {snippet!r}")


def extract_section(client: anthropic.Anthropic, section_bytes: bytes,
                     start_1based: int, end_1based: int, depth: int = 0) -> list:
    """Extracts every recipe in a page range. On truncation (stop_reason !=
    'end_turn') or a JSON parse failure, bisects the range and retries as two
    smaller calls, rather than aborting or accepting a silently-partial result."""
    indent = "  " * (depth + 1)
    n_pages = end_1based - start_1based + 1

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf",
                        "data": base64.b64encode(section_bytes).decode(),
                    }},
                    {"type": "text", "text": "Extract every complete recipe on these pages as a JSON array."},
                ],
            }],
        )
    except anthropic.APIError as exc:
        print(f"{indent}API error on pages {start_1based}-{end_1based}: {exc}")
        raise

    raw_text = "".join(b.text for b in message.content if b.type == "text")

    truncated = message.stop_reason != "end_turn"
    parsed = None
    if not truncated:
        try:
            parsed = _extract_json_array(raw_text)
        except json.JSONDecodeError:
            truncated = True  # treat "claims to be done but isn't valid JSON" the same way

    if not truncated:
        print(f"{indent}pages {start_1based}-{end_1based}: {len(parsed)} recipe(s) "
              f"(stop_reason={message.stop_reason})")
        return parsed

    if n_pages <= 1:
        raise RuntimeError(
            f"Section pages {start_1based}-{end_1based} truncated (stop_reason="
            f"{message.stop_reason!r}) and can't be split further (single page)."
        )

    mid = start_1based + n_pages // 2 - 1
    print(f"{indent}pages {start_1based}-{end_1based} truncated (stop_reason="
          f"{message.stop_reason!r}) - bisecting into {start_1based}-{mid} and {mid + 1}-{end_1based}")
    reader = PdfReader(io.BytesIO(section_bytes))
    # section_bytes is a standalone PDF (written by PdfWriter, no cover-page
    # offset) - its own page 0 really is index 0, so this uses the plain
    # 0-based slicer, not split_pdf_pages().
    split_at = mid - start_1based + 1  # how many pages belong to the first half
    first_half = _slice_pdf_0based(reader, 0, split_at - 1)
    second_half = _slice_pdf_0based(reader, split_at, n_pages - 1)
    return (
        extract_section(client, first_half, start_1based, mid, depth + 1)
        + extract_section(client, second_half, mid + 1, end_1based, depth + 1)
    )


def assign_category(section: dict, name: str) -> tuple:
    """Returns (category, problem_or_None)."""
    if section["category"] is not None:
        return section["category"], None
    key = " ".join(name.split()).strip().lower()
    if key in SALADS_SIDES_CATEGORY:
        return SALADS_SIDES_CATEGORY[key], None
    return "Side", f"Salads & Sides recipe {name!r} didn't match the hardcoded lookup table"


def build_row(raw: dict, section: dict) -> tuple:
    """Applies house style + category/cuisine assignment. Returns (row, problems)."""
    problems = []

    name = transform("name", (raw.get("name") or "").strip())
    submitter_name = (raw.get("submitter_name") or "").strip()
    if not submitter_name:
        problems.append(f"{name!r}: blank submitter_name")

    category, cat_problem = assign_category(section, raw.get("name") or "")
    if cat_problem:
        problems.append(cat_problem)

    cuisine = (raw.get("cuisine") or "").strip()
    if cuisine not in CUISINES:
        cuisine = "Other" if cuisine else ""

    ingredients_list = raw.get("ingredients") or []
    instructions_list = raw.get("instructions") or []
    if isinstance(ingredients_list, str):
        ingredients_list = [ingredients_list]
    if isinstance(instructions_list, str):
        instructions_list = [instructions_list]

    ingredients = transform("ingredients", "\n".join(
        str(i).strip() for i in ingredients_list if str(i).strip()))
    instructions = transform("instructions", "\n".join(
        str(s).strip() for s in instructions_list if str(s).strip()))
    story = transform("story", (raw.get("story") or "").strip())

    row = {
        "name": name,
        "cookbook": "d1",
        "submitter_name": submitter_name,
        "category": category,
        "cuisine": cuisine,
        "dietary_tags": "",
        "prep_time": (raw.get("prep_time") or "").strip(),
        "servings": (raw.get("servings") or "").strip(),
        "ingredients": ingredients,
        "instructions": instructions,
        "story": story,
        "photo_path": None,
        "source_image_path": "",
        "source_url": "",
        "status": "published",
        "submitted_at": D1_COMPILE_DATE,
        "reviewed_at": None,
        "parse_model": MODEL,
        "proofreading_notes": "",
    }
    return row, problems


# ----------------------------------------------------------------- checks --

_RESIDUE_PATTERNS = {
    "bare TB": re.compile(r"\bTB\b"),
    "bare tsp (no period)": re.compile(r"\btsp\b(?!\.)"),
    "old-style T. (not Tbsp.)": re.compile(r"\bT\.(?!bsp)"),
    "old-style t. (not tsp.)": re.compile(r"\bt\.(?!sp)"),
    "leftover vulgar fraction": re.compile(r"[¼½¾⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]"),
}


def _norm_name(s: str) -> str:
    return " ".join(s.replace("’", "'").split()).strip().lower()


def check_names_by_section(sections_data: dict) -> list:
    """Per-name diff against the hand-transcribed TOC, not just an aggregate
    count. A recipe that vanishes entirely at a bisection boundary (no
    orphaned fragment, no blank submitter - just silently gone) is invisible
    to every other check here, and can even leave the aggregate count
    correct by coincidence if an unrelated duplicate elsewhere cancels it
    out. This is what actually caught it. See TOC_NAMES' docstring-comment
    for why some diffs are accepted source-quirk aliases, not real problems."""
    problems = []
    for section_name, expected_names in TOC_NAMES.items():
        actual_names = [_norm_name(r["name"]) for r in sections_data.get(section_name, [])]
        expected_norm = [_norm_name(n) for n in expected_names]

        exp_counts, act_counts = {}, {}
        for n in expected_norm:
            exp_counts[n] = exp_counts.get(n, 0) + 1
        for n in actual_names:
            act_counts[n] = act_counts.get(n, 0) + 1

        for n, c in exp_counts.items():
            short = act_counts.get(n, 0)
            if short < c:
                problems.append(
                    f"MISSING in {section_name}: {n!r} (expected {c}, found {short}) "
                    f"- check for a recipe silently lost at a bisection boundary")
        for n, c in act_counts.items():
            over = c - exp_counts.get(n, 0)
            if over > 0:
                problems.append(
                    f"UNEXPECTED in {section_name}: {n!r} x{over} - not in the TOC name "
                    f"list; check for an orphaned fragment wrongly promoted to its own recipe")
    return problems


def run_checks(all_rows: list, section_counts: dict, sections_data: dict) -> list:
    problems = []

    for section in SECTIONS:
        expected = section["expected"]
        actual = section_counts.get(section["name"], 0)
        if actual != expected:
            problems.append(
                f"COUNT MISMATCH in {section['name']}: expected {expected}, got {actual}")

    problems.extend(check_names_by_section(sections_data))

    for row in all_rows:
        label = f"{row['name']!r} (by {row['submitter_name']!r})"
        for field in ("ingredients", "instructions"):
            text = row[field]
            for label_pat, pat in _RESIDUE_PATTERNS.items():
                if pat.search(text):
                    problems.append(f"{label}: leftover {label_pat} in {field}")

    name_counts = {}
    for row in all_rows:
        key = " ".join(row["name"].split()).strip().lower()
        name_counts[key] = name_counts.get(key, 0) + 1
    for key, expected in EXPECTED_NAME_COUNTS.items():
        actual = name_counts.get(key, 0)
        if actual != expected:
            problems.append(f"DUPLICATE-NAME MISMATCH for {key!r}: expected {expected}, got {actual}")

    return problems


# --------------------------------------------------------------- pipeline --

def run_dry(pdf_path: str, only_sections: list = None) -> None:
    reader = PdfReader(pdf_path)
    client = _client()

    existing = {}
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as fh:
            existing = json.load(fh)
        print(f"Resuming - {len(existing)} section(s) already in {OUTPUT_PATH}")

    all_problems = []
    for section in SECTIONS:
        if only_sections and section["name"] not in only_sections:
            continue
        if section["name"] in existing:
            print(f"\n{section['name']}: already done ({len(existing[section['name']])} recipes), skipping")
            continue

        print(f"\n{section['name']} (pages {section['start']}-{section['end']}):")
        preview_chunk(reader, section["start"], section["end"])

        section_bytes = split_pdf_pages(reader, section["start"], section["end"])
        raw_recipes = extract_section(client, section_bytes, section["start"], section["end"])

        rows = []
        for raw in raw_recipes:
            row, problems = build_row(raw, section)
            rows.append(row)
            all_problems.extend(problems)

        existing[section["name"]] = rows
        with open(OUTPUT_PATH, "w") as fh:
            json.dump(existing, fh, indent=2, ensure_ascii=False)
        print(f"  -> wrote {len(rows)} recipe(s) to {OUTPUT_PATH}")

    all_rows = [row for rows in existing.values() for row in rows]
    section_counts = {name: len(rows) for name, rows in existing.items()}
    all_problems.extend(run_checks(all_rows, section_counts, existing))

    print(f"\n{'=' * 70}")
    print(f"{len(all_rows)} recipe(s) across {len(existing)} section(s)")
    if all_problems:
        print(f"\n{len(all_problems)} PROBLEM(S) FLAGGED:")
        for p in all_problems:
            print(f"  - {p}")
    else:
        print("\nNo problems flagged by automated checks.")
    print(f"\nFull output: {OUTPUT_PATH}")


def run_commit(database_url: str, reset_first: bool) -> None:
    import psycopg
    from psycopg.rows import dict_row
    import db as db_module

    if not os.path.exists(OUTPUT_PATH):
        sys.exit(f"No {OUTPUT_PATH} found - run a dry-run first.")
    with open(OUTPUT_PATH) as fh:
        sections = json.load(fh)
    all_rows = [row for rows in sections.values() for row in rows]
    if not all_rows:
        sys.exit("Output file has no recipes - nothing to commit.")

    # This database is shared with the University Ward and Bethers Family
    # cookbooks (see cookbooks.py) - init_db() runs the full, idempotent
    # migration chain (including the cookbook column) rather than just
    # SCHEMA, so this is safe to run against a database that already has
    # other cookbooks' data in it.
    os.environ["DATABASE_URL"] = database_url
    db_module.init_db()

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        if reset_first:
            # Scoped to this cookbook only - a bare TRUNCATE here would wipe
            # every cookbook sharing this database, not just D1's rows.
            # ON DELETE CASCADE on reviews/dish_photos/recipe_edits' FKs
            # cleans those up too.
            deleted = conn.execute("DELETE FROM recipes WHERE cookbook = 'd1'")
            conn.commit()
            print(f"Deleted {deleted.rowcount} existing D1 recipe(s).")

        cols = list(all_rows[0].keys())
        placeholders = ", ".join(f"%({c})s" for c in cols)
        sql = f"INSERT INTO recipes ({', '.join(cols)}) VALUES ({placeholders})"
        with conn.cursor() as cur:
            cur.executemany(sql, all_rows)
        conn.commit()
        print(f"Inserted {len(all_rows)} recipe(s) into {database_url.split('@')[-1]} (cookbook='d1').")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", default=PDF_PATH)
    parser.add_argument("--sections", help="comma-separated section names to (re)run")
    parser.add_argument("--commit", action="store_true", help="write to DATABASE_URL for real")
    parser.add_argument(
        "--reset-first", action="store_true",
        help="delete D1's existing recipes (cookbook='d1' only) before inserting",
    )
    args = parser.parse_args()

    only_sections = [s.strip() for s in args.sections.split(",")] if args.sections else None

    if args.commit:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            sys.exit("--commit requires DATABASE_URL to be set.")
        run_commit(database_url, args.reset_first)
    else:
        run_dry(args.pdf, only_sections)


if __name__ == "__main__":
    main()
