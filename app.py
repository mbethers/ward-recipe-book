"""University Ward Cookbook - Flask app.

Public: browse/search published recipes, submit a recipe (typed or photo/PDF upload).
Admin (/admin, password-gated): review pending submissions, edit, approve/reject.
"""
import difflib
import functools
import json
import os
import re
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()  # must run before importing db/storage, which read env vars at import time
except ImportError:
    pass

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg

import db
from ai_parse import ParseError, parse_recipe, parse_recipe_multi, proofread_recipe, propose_recipe_fix, apply_recipe_fix_choice
import image_utils
import email_utils
import storage
from web_recipe import FetchError, parse_recipe_from_url

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-not-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB
# There are no CSRF tokens anywhere in this app. SameSite=Lax keeps the admin
# session cookie off cross-site POSTs, which matters most for the correction
# approve route - that's the one place where an outsider supplies the payload,
# so a forged approve would publish their text without it ever being read.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

db.register(app)

WARD_NAME = os.environ.get("WARD_NAME", "University Ward")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


with app.app_context():
    db.init_db()


# ---------------------------------------------------------------- helpers --

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _notify_admin(subject: str, body: str) -> None:
    """Best-effort email to the admin - never lets a notification failure
    affect the submission that triggered it."""
    try:
        email_utils.send_admin_notification(subject, body)
    except RuntimeError as exc:
        app.logger.warning("Admin notification email not sent: %s", exc)


# --------------------------------------------- public correction suggestions --

# What a visitor is allowed to propose. submitter_name is deliberately absent:
# a correction can fix the recipe but must never reassign who it's credited to.
# The approve SQL builds its column list from this tuple, never from the keys of
# the stored JSON.
SUGGESTABLE_FIELDS = (
    "name", "category", "cuisine", "dietary_tags",
    "prep_time", "servings", "ingredients", "instructions", "story",
)
FIELD_LABELS = {
    "name": "Recipe name", "category": "Category", "cuisine": "Cuisine",
    "dietary_tags": "Dietary", "prep_time": "Total prep time", "servings": "Servings",
    "ingredients": "Ingredients", "instructions": "Instructions", "story": "Story",
}
MULTILINE_FIELDS = ("ingredients", "instructions", "story")
# Emptying these would gut the recipe, so a blank value means "untouched".
REQUIRED_FIELDS = ("name", "ingredients", "instructions")
# MAX_CONTENT_LENGTH is a whole-body limit, so a 14MB urlencoded POST would sail
# into a TEXT column. These are per-field, and over-length is refused outright -
# silently truncating someone's recipe is the failure mode this app keeps hitting.
FIELD_MAX = {
    "name": 200, "category": 60, "cuisine": 60, "dietary_tags": 120,
    "prep_time": 100, "servings": 100,
    "ingredients": 10000, "instructions": 20000, "story": 5000,
}
MAX_PENDING_PER_RECIPE = 5


def _norm(text) -> str:
    """Normalize a submitted field for comparison and storage.

    Browsers submit <textarea> content with CRLF line endings while the database
    holds LF. Without this every untouched textarea reads as changed, so the
    no-op check never fires and every diff shows every line replaced.
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _collect_proposed(form, recipe) -> tuple:
    """Works out which fields a suggestion actually changes.

    Returns (proposed, base, error). `proposed` holds only the fields that
    differ from the live recipe, `base` the live values of those same keys.
    `error` is a message to show the visitor, or '' when the suggestion is fine.

    A value that isn't in the allowed list for category/cuisine/dietary counts as
    untouched rather than being coerced to a default - coercing would show the
    admin a confident-looking "American -> Other" that the suggester never asked
    for.
    """
    proposed, base = {}, {}

    for field in SUGGESTABLE_FIELDS:
        if field == "dietary_tags":
            # Absent entirely means the form never offered it; present but empty
            # legitimately means "clear the tags".
            if "dietary" not in form:
                continue
            value = ",".join(t for t in form.getlist("dietary") if t in db.DIETARY_TAGS)
        else:
            if field not in form:
                continue
            value = _norm(form.get(field))

        if len(value) > FIELD_MAX[field]:
            return {}, {}, (
                f"That {FIELD_LABELS[field].lower()} is too long "
                f"({len(value)} characters, limit {FIELD_MAX[field]})."
            )
        if field == "category" and value and value not in db.CATEGORIES:
            continue
        if field == "cuisine" and value and value not in db.CUISINES:
            continue

        live = _norm(recipe[field])
        if value == live:
            continue
        if field in REQUIRED_FIELDS and not value:
            return {}, {}, f"{FIELD_LABELS[field]} can't be left empty."

        proposed[field] = value
        base[field] = live

    if not proposed:
        return {}, {}, "Nothing looked different from the recipe as it stands."
    return proposed, base, ""


def _diff_hunks(before: str, after: str) -> list:
    """Line-level diff of one field -> [{'old': [...], 'new': [...]}, ...].

    One entry per changed region; unchanged lines are dropped, since the review
    screen shows the complete before/after separately. SequenceMatcher rather
    than ndiff (which emits '? ' hint lines) or unified_diff (which emits @@
    headers) - both would need stripping before display.
    """
    old_lines, new_lines = (before or "").splitlines(), (after or "").splitlines()
    hunks = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            hunks.append({"old": old_lines[i1:i2], "new": new_lines[j1:j2]})
    return hunks


def _build_field_diffs(recipe, proposed: dict, base: dict) -> list:
    """One entry per changed field, for the admin review screen."""
    diffs = []
    for field in SUGGESTABLE_FIELDS:
        if field not in proposed:
            continue
        live = _norm(recipe[field])
        diffs.append({
            "field": field,
            "label": FIELD_LABELS[field],
            "multiline": field in MULTILINE_FIELDS,
            "before": live,
            "after": proposed[field],
            "hunks": _diff_hunks(live, proposed[field]) if field in MULTILINE_FIELDS else [],
            # The recipe moved on since this was suggested - the admin should
            # know the diff isn't against what the suggester was looking at.
            "stale": base.get(field, live) != live,
        })
    return diffs


# Phrases that mean Claude described the edit instead of writing the replacement
# text. These read as editorial instructions and effectively never occur in an
# actual recipe, so seeing one means the "fix" would paste commentary into the
# cookbook - which is exactly what happened to the Veggie Weggie instructions.
_EDITORIAL_PHRASES = re.compile(
    r"\b("
    r"keeping only|the core instruction|copy[- ]paste|duplicate fragment|"
    r"should be (changed|replaced|rewritten)|corrected version|here is the corrected|"
    r"verify the complete|remove the duplicate|the entire (line|section) should"
    r")\b",
    re.IGNORECASE,
)


def _reject_reason(original_snippet: str, fixed_snippet: str) -> str:
    """Returns why a proposed replacement must not be written, or '' if it's safe.

    A fix edits recipe text. Anything that reads like a description of an edit,
    or that collapses a multi-line list into a single line, is a malfunction -
    and writing it destroys the recipe."""
    if _EDITORIAL_PHRASES.search(fixed_snippet or ""):
        return ("the replacement reads as a description of the change rather than "
                "the corrected recipe text")

    # A deletion (empty replacement) is fine and is handled by the size check.
    if (fixed_snippet or "").strip():
        original_lines = len((original_snippet or "").strip().splitlines())
        fixed_lines = len(fixed_snippet.strip().splitlines())
        if original_lines >= 3 and fixed_lines == 1:
            return (f"it would collapse {original_lines} lines into a single line")
    return ""


def _normalize_issue(text: str) -> str:
    """Collapse whitespace and curly quotes so an issue posted back through a
    form still matches the line stored in the database."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip().lower()


def _drop_issue(stored_notes: str, issue_text: str, issue_index: str = "") -> str:
    """Removes one issue from the stored proofreading notes.

    Matches on normalized text first. Falls back to the row's index (carried
    through the form) so a resolved issue still disappears even if its text
    came back altered - otherwise the admin sees an issue they just fixed
    sitting there with a Fix button on it."""
    lines = [n.strip() for n in (stored_notes or "").split("\n") if n.strip()]
    target = _normalize_issue(issue_text)

    remaining = [n for n in lines if _normalize_issue(n) != target]
    if len(remaining) == len(lines):  # nothing matched - fall back to the index
        try:
            idx = int(issue_index)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(lines):
            remaining = lines[:idx] + lines[idx + 1:]

    return "\n".join(remaining)


def _run_proofreading(name: str, ingredients: str, instructions: str, story: str) -> str:
    """Flags likely typos (e.g. "Belgium Waffles") for the admin to see during
    review - suggestions only, nothing is auto-changed. proofread_recipe()
    already never raises, so this is just here to turn a list into storage
    format."""
    issues = proofread_recipe(name, ingredients, instructions, story)
    return "\n".join(issues)


@app.context_processor
def inject_globals():
    return {"ward_name": WARD_NAME}


_BULLET_PREFIX = re.compile(r"^[•◦▪‣∙·*»]\s+|^[-–—]\s+")
_NUMBERED_PREFIX = re.compile(r"^(?:step\s*)?\d+[.):]\s+", re.IGNORECASE)


def _strip_pasted_marker(text: str) -> str:
    """Removes a leading bullet/number marker a pasted-in line already had -
    our own <li>/<ol> already draws one, so a leftover one shows as a double
    bullet/number (e.g. a pasted "- 2 cups flour" under an already-bulleted <li>)."""
    text = (text or "").strip()
    text = _BULLET_PREFIX.sub("", text)
    text = _NUMBERED_PREFIX.sub("", text)
    return text.strip()


@app.template_filter("clean_step")
def clean_step(line):
    return _strip_pasted_marker(line)


@app.template_filter("clean_ingredient")
def clean_ingredient(line):
    """Same cleanup as clean_step, plus flags section labels like "Red Sauce:"
    so the template can render them as an unbulleted sub-heading instead of
    an ingredient with its own bullet."""
    text = _strip_pasted_marker(line)
    is_header = text.endswith(":") and len(text) <= 40
    return {"text": text, "is_header": is_header}


@app.template_filter("shortdate")
def shortdate(iso_string):
    """'2026-08-16T22:31:00+00:00' -> '8/16/26' (no leading zeros, 2-digit year)."""
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return ""
    return f"{dt.month}/{dt.day}/{dt.strftime('%y')}"


# ------------------------------------------------------------- public UI --

@app.route("/")
def index():
    conn = db.get_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    cuisine = request.args.get("cuisine", "").strip()
    active_dietary = [t for t in request.args.getlist("dietary") if t in db.DIETARY_TAGS]

    sql = """SELECT r.*,
                    COALESCE(AVG(rv.rating), 0)::float AS avg_rating,
                    COUNT(rv.id) AS review_count,
                    (SELECT dp.photo_path FROM dish_photos dp
                     WHERE dp.recipe_id = r.id AND dp.status = 'published'
                     ORDER BY dp.submitted_at ASC LIMIT 1) AS gallery_photo
             FROM recipes r
             LEFT JOIN reviews rv ON rv.recipe_id = r.id
             WHERE r.status = 'published'"""
    params = []
    if q:
        sql += " AND (r.name ILIKE %s OR r.ingredients ILIKE %s OR r.submitter_name ILIKE %s)"
        like = f"%{q}%"
        params += [like, like, like]
    if category:
        sql += " AND r.category = %s"
        params.append(category)
    if cuisine:
        sql += " AND r.cuisine = %s"
        params.append(cuisine)
    for tag in active_dietary:
        # Each selected tag must be present - "Vegan" + "Gluten-free" means both.
        sql += " AND r.dietary_tags ILIKE %s"
        params.append(f"%{tag}%")
    sql += " GROUP BY r.id ORDER BY LOWER(r.name) ASC"

    recipes = conn.execute(sql, params).fetchall()

    # Precompute each dietary chip's toggle URL (add/remove itself from the
    # active set while keeping q/category/cuisine and the other active tags).
    dietary_chips = []
    for tag in db.DIETARY_TAGS:
        is_active = tag in active_dietary
        next_tags = [t for t in active_dietary if t != tag] if is_active else active_dietary + [tag]
        dietary_chips.append({
            "tag": tag,
            "active": is_active,
            "url": url_for("index", q=q, category=category, cuisine=cuisine, dietary=next_tags),
        })

    return render_template(
        "index.html",
        recipes=recipes,
        categories=db.CATEGORIES,
        cuisines=db.CUISINES,
        dietary_chips=dietary_chips,
        q=q,
        active_category=category,
        active_cuisine=cuisine,
        intro_text=db.get_setting("intro_text", db.DEFAULT_INTRO_TEXT),
    )


@app.route("/recipe/<int:recipe_id>")
def recipe_detail(recipe_id):
    conn = db.get_db()
    recipe = conn.execute(
        "SELECT * FROM recipes WHERE id = %s AND status = 'published'", (recipe_id,)
    ).fetchone()
    if not recipe:
        abort(404)
    reviews = conn.execute(
        "SELECT * FROM reviews WHERE recipe_id = %s ORDER BY created_at DESC", (recipe_id,)
    ).fetchall()
    avg_rating = sum(r["rating"] for r in reviews) / len(reviews) if reviews else 0
    dish_photos = conn.execute(
        """SELECT * FROM dish_photos WHERE recipe_id = %s AND status = 'published'
           ORDER BY submitted_at ASC""",
        (recipe_id,),
    ).fetchall()
    return render_template(
        "recipe.html", recipe=recipe, reviews=reviews, avg_rating=avg_rating, dish_photos=dish_photos
    )


@app.route("/recipe/<int:recipe_id>/review", methods=["POST"])
def submit_review(recipe_id):
    conn = db.get_db()
    recipe = conn.execute(
        "SELECT id FROM recipes WHERE id = %s AND status = 'published'", (recipe_id,)
    ).fetchone()
    if not recipe:
        abort(404)

    reviewer_name = (request.form.get("reviewer_name") or "").strip()
    try:
        rating = int(request.form.get("rating", ""))
    except ValueError:
        rating = 0

    if not reviewer_name or rating < 1 or rating > 5:
        flash("Please enter your name and pick a star rating.")
        return redirect(url_for("recipe_detail", recipe_id=recipe_id) + "#reviews")

    conn.execute(
        """INSERT INTO reviews (recipe_id, reviewer_name, rating, comment, created_at)
           VALUES (%s, %s, %s, %s, %s)""",
        (recipe_id, reviewer_name, rating, (request.form.get("comment") or "").strip(), db.now_iso()),
    )
    conn.commit()
    flash("Thanks for your review!")
    return redirect(url_for("recipe_detail", recipe_id=recipe_id) + "#reviews")


@app.route("/recipe/<int:recipe_id>/photo", methods=["POST"])
def submit_dish_photo(recipe_id):
    """A photo of the finished dish, independent of any review. Goes into the
    same pending -> admin-approved pipeline as recipes - nothing a visitor
    uploads appears publicly without an admin approving it first."""
    conn = db.get_db()
    recipe = conn.execute(
        "SELECT id FROM recipes WHERE id = %s AND status = 'published'", (recipe_id,)
    ).fetchone()
    if not recipe:
        abort(404)

    uploader_name = (request.form.get("uploader_name") or "").strip()
    upload = request.files.get("photo")
    anchor = url_for("recipe_detail", recipe_id=recipe_id) + "#photos"

    if not uploader_name or not upload or not upload.filename:
        flash("Please enter your name and choose a photo.")
        return redirect(anchor)
    if not image_utils.is_image(upload.filename):
        flash("Please upload a photo file (jpg, png, etc).")
        return redirect(anchor)

    try:
        jpeg_bytes = image_utils.normalize_image(upload.read())
    except image_utils.UnsupportedImageError as exc:
        flash(str(exc))
        return redirect(anchor)

    if not storage.storage_configured():
        flash("Photo storage isn't set up yet - try again later.")
        return redirect(anchor)
    try:
        photo_url = storage.upload_bytes(jpeg_bytes, "photo.jpg", "image/jpeg", "gallery")
    except RuntimeError as exc:
        app.logger.error("Supabase upload failed: %s", exc)
        flash("That photo couldn't be uploaded - please try again.")
        return redirect(anchor)

    conn.execute(
        """INSERT INTO dish_photos (recipe_id, uploader_name, photo_path, status, submitted_at)
           VALUES (%s, %s, %s, 'pending', %s)""",
        (recipe_id, uploader_name, photo_url, db.now_iso()),
    )
    conn.commit()
    _notify_admin(
        "New dish photo pending review",
        f"{uploader_name} uploaded a photo for review.\n\n"
        f"Review it: {url_for('admin_photos', _external=True)}",
    )
    flash("Thanks! Your photo will show up here once an admin approves it.")
    return redirect(anchor)


def _published_recipe_or_404(conn, recipe_id):
    recipe = conn.execute(
        "SELECT * FROM recipes WHERE id = %s AND status = 'published'", (recipe_id,)
    ).fetchone()
    if not recipe:
        abort(404)
    return recipe


@app.route("/recipe/<int:recipe_id>/suggest", methods=["GET"])
def suggest_edit_form(recipe_id):
    conn = db.get_db()
    recipe = _published_recipe_or_404(conn, recipe_id)
    active_dietary = set(recipe["dietary_tags"].split(",")) if recipe["dietary_tags"] else set()
    return render_template(
        "suggest_edit.html",
        recipe=recipe,
        categories=db.CATEGORIES,
        cuisines=db.CUISINES,
        dietary_options=db.DIETARY_TAGS,
        active_dietary=active_dietary,
    )


@app.route("/recipe/<int:recipe_id>/suggest", methods=["POST"])
def suggest_edit(recipe_id):
    """Records a proposed correction. Writes nothing to the recipe itself -
    the published version only ever changes from admin_correction_approve."""
    conn = db.get_db()
    recipe = _published_recipe_or_404(conn, recipe_id)
    back = url_for("suggest_edit_form", recipe_id=recipe_id)

    # Honeypot: a real person never fills a field they can't see.
    if (request.form.get("website") or "").strip():
        app.logger.info("Discarded honeypot correction for recipe %s", recipe_id)
        flash("Thanks! An admin will look over your suggestion.")
        return redirect(url_for("recipe_detail", recipe_id=recipe_id))

    suggester_name = (request.form.get("suggester_name") or "").strip()
    note = (request.form.get("note") or "").strip()
    if not suggester_name:
        flash("Please add your name so the admin knows who suggested this.")
        return redirect(back)
    if len(suggester_name) > 80 or len(note) > 1000:
        flash("That name or note is longer than we can store - please shorten it.")
        return redirect(back)

    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM recipe_edits WHERE recipe_id = %s AND status = 'pending'",
        (recipe_id,),
    ).fetchone()["n"]
    if pending >= MAX_PENDING_PER_RECIPE:
        flash("There are already several suggestions waiting on this recipe - "
              "please give the admin a chance to work through them first.")
        return redirect(url_for("recipe_detail", recipe_id=recipe_id))

    proposed, base, error = _collect_proposed(request.form, recipe)
    if error:
        flash(error)
        return redirect(back)

    conn.execute(
        """INSERT INTO recipe_edits
           (recipe_id, suggester_name, note, proposed, base, status, submitted_at)
           VALUES (%s, %s, %s, %s, %s, 'pending', %s)""",
        (recipe_id, suggester_name, note,
         json.dumps(proposed), json.dumps(base), db.now_iso()),
    )
    conn.commit()

    # Link only. The proposed text is supplied by the public and can be long -
    # it belongs on the review screen, not in the admin's inbox.
    _notify_admin(
        f'Correction suggested for "{recipe["name"]}"',
        f'{suggester_name} suggested a correction to "{recipe["name"]}".\n\n'
        f"Review it: {url_for('admin_corrections', _external=True)}",
    )
    flash("Thanks! Your suggestion goes to an admin - the recipe stays as it is until they approve it.")
    return redirect(url_for("recipe_detail", recipe_id=recipe_id))


@app.route("/submit", methods=["GET"])
def submit_form():
    return render_template(
        "submit.html", categories=db.CATEGORIES, cuisines=db.CUISINES, dietary_options=db.DIETARY_TAGS
    )


@app.route("/submit/text", methods=["POST"])
def submit_text():
    form = request.form
    name = (form.get("name") or "").strip()
    submitter_name = (form.get("submitter_name") or "").strip()
    if not name or not submitter_name:
        flash("Recipe name and your name are required.")
        return redirect(url_for("submit_form"))

    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    cuisine = form.get("cuisine") or ""
    if cuisine not in db.CUISINES:
        cuisine = ""

    dietary_tags = ",".join(t for t in form.getlist("dietary") if t in db.DIETARY_TAGS)
    prep_time = (form.get("prep_time") or "").strip()
    servings = (form.get("servings") or "").strip()

    photo_url = None
    photo = request.files.get("photo")
    if photo and photo.filename:
        photo_url = _store_dish_photo(photo)

    ingredients = (form.get("ingredients") or "").strip()
    instructions = (form.get("instructions") or "").strip()
    story = (form.get("story") or "").strip()
    proofreading_notes = _run_proofreading(name, ingredients, instructions, story)

    conn = db.get_db()
    new_id = conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, cuisine, dietary_tags, prep_time, servings,
            ingredients, instructions, story, photo_path, status, submitted_at, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
           RETURNING id""",
        (
            name,
            submitter_name,
            category,
            cuisine,
            dietary_tags,
            prep_time,
            servings,
            ingredients,
            instructions,
            story,
            photo_url,
            db.now_iso(),
            proofreading_notes,
        ),
    ).fetchone()["id"]
    conn.commit()
    _notify_admin(
        f'New recipe pending review: "{name}"',
        f"{submitter_name} submitted \"{name}\" for review.\n\n"
        f"Review it: {url_for('admin_recipe_edit', recipe_id=new_id, _external=True)}",
    )
    return redirect(url_for("submitted"))


@app.route("/submit/photo", methods=["POST"])
def submit_photo():
    form = request.form
    submitter_name = (form.get("submitter_name") or "").strip()
    uploads = request.files.getlist("source_files")

    if not submitter_name or not uploads or not any(u.filename for u in uploads):
        flash("Your name and at least one photo or PDF are required.")
        return redirect(url_for("submit_form"))

    # Validate file count
    uploads = [u for u in uploads if u.filename]
    if len(uploads) > 3:
        flash("Please upload at most 3 files.")
        return redirect(url_for("submit_form"))

    # Process all files
    source_image_paths = []
    parse_bytes_list = []  # For Claude - pairs of (bytes, media_type)

    for upload in uploads:
        raw_bytes = upload.read()
        filename = upload.filename

        if image_utils.is_pdf(filename):
            media_type = "application/pdf"
            parse_bytes = raw_bytes
            store_bytes, store_content_type, store_ext = raw_bytes, "application/pdf", ".pdf"
        elif image_utils.is_image(filename):
            media_type = "image/jpeg"
            try:
                parse_bytes = image_utils.normalize_image(raw_bytes)
            except image_utils.UnsupportedImageError as exc:
                flash(f"Couldn't read {filename}: {exc}")
                return redirect(url_for("submit_form"))
            store_bytes, store_content_type, store_ext = parse_bytes, "image/jpeg", ".jpg"
        else:
            flash(f"{filename} is not a photo (jpg/png/heic) or PDF.")
            return redirect(url_for("submit_form"))

        # Store the source file
        source_image_path = None
        if storage.storage_configured():
            try:
                source_image_path = storage.upload_bytes(
                    store_bytes, f"source{store_ext}", store_content_type, "sources"
                )
                source_image_paths.append(source_image_path)
            except RuntimeError as exc:
                app.logger.error("Supabase upload failed for %s: %s", filename, exc)

        # Collect for Claude parsing
        parse_bytes_list.append((parse_bytes, media_type))

    # Parse all files together with Claude
    parsed = {
        "name": "",
        "category": "Other",
        "cuisine": "",
        "prep_time": "",
        "servings": "",
        "ingredients": "",
        "instructions": "",
        "story": "",
        "parse_model": None,
    }
    try:
        parsed = parse_recipe_multi(parse_bytes_list)
    except ParseError as exc:
        app.logger.error("Claude parse failed: %s", exc)
        flash("We couldn't automatically read that - no worries, just fill in what you can below.")

    # Store paths as newline-separated string
    source_image_path_str = "\n".join(source_image_paths) if source_image_paths else ""

    return render_template(
        "submit_preview.html",
        submitter_name=submitter_name,
        source_url="",
        source_image_path=source_image_path_str,
        source_image_paths=source_image_paths,
        parsed=parsed,
        categories=db.CATEGORIES,
        cuisines=db.CUISINES,
        dietary_options=db.DIETARY_TAGS,
    )


@app.route("/submit/photo/confirm", methods=["POST"])
def submit_photo_confirm():
    """Takes the (possibly hand-edited) fields from the review screen after
    a photo/PDF upload and actually creates the pending submission - the
    upload/parse already happened in submit_photo() above."""
    form = request.form
    name = (form.get("name") or "").strip()
    submitter_name = (form.get("submitter_name") or "").strip()
    source_image_path = (form.get("source_image_path") or "").strip() or None
    if not name or not submitter_name:
        flash("Recipe name and your name are required.")
        return redirect(url_for("submit_form"))

    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    cuisine = form.get("cuisine") or ""
    if cuisine not in db.CUISINES:
        cuisine = ""

    dietary_tags = ",".join(t for t in form.getlist("dietary") if t in db.DIETARY_TAGS)
    prep_time = (form.get("prep_time") or "").strip()
    servings = (form.get("servings") or "").strip()
    ingredients = (form.get("ingredients") or "").strip()
    instructions = (form.get("instructions") or "").strip()
    story = (form.get("story") or "").strip()
    proofreading_notes = _run_proofreading(name, ingredients, instructions, story)

    conn = db.get_db()
    new_id = conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, cuisine, dietary_tags, prep_time, servings,
            ingredients, instructions, story, source_image_path, status, submitted_at,
            parse_model, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
           RETURNING id""",
        (
            name,
            submitter_name,
            category,
            cuisine,
            dietary_tags,
            prep_time,
            servings,
            ingredients,
            instructions,
            story,
            source_image_path,
            db.now_iso(),
            (form.get("parse_model") or "").strip() or None,
            proofreading_notes,
        ),
    ).fetchone()["id"]
    conn.commit()
    _notify_admin(
        f'New recipe pending review: "{name}"',
        f"{submitter_name} submitted \"{name}\" (from a photo/PDF upload) for review.\n\n"
        f"Review it: {url_for('admin_recipe_edit', recipe_id=new_id, _external=True)}",
    )
    return redirect(url_for("submitted"))


@app.route("/submit/url", methods=["POST"])
def submit_url():
    form = request.form
    submitter_name = (form.get("submitter_name") or "").strip()
    recipe_url = (form.get("recipe_url") or "").strip()
    if not submitter_name or not recipe_url:
        flash("Your name and a recipe link are both required.")
        return redirect(url_for("submit_form"))

    try:
        parsed, final_url = parse_recipe_from_url(recipe_url)
    except FetchError as exc:
        flash(str(exc))
        return redirect(url_for("submit_form"))
    except ParseError as exc:
        # We reached the page but couldn't make a recipe out of it - hand
        # back a blank preview so the submitter can just fill it in
        # themselves rather than leaving it entirely to the admin.
        app.logger.error("Web recipe parse failed for %s: %s", recipe_url, exc)
        parsed = {"name": "", "category": "Other", "cuisine": "", "prep_time": "", "servings": "",
                  "ingredients": "", "instructions": "", "story": "", "parse_model": None}
        final_url = recipe_url
        flash("We couldn't automatically read that page - no worries, just fill in what you can below.")

    return render_template(
        "submit_preview.html",
        submitter_name=submitter_name,
        source_url=final_url,
        source_image_path="",
        parsed=parsed,
        categories=db.CATEGORIES,
        cuisines=db.CUISINES,
        dietary_options=db.DIETARY_TAGS,
    )


@app.route("/submit/url/confirm", methods=["POST"])
def submit_url_confirm():
    """Takes the (possibly hand-edited) fields from the review screen after
    a link import and actually creates the pending submission - the fetch/
    parse already happened in submit_url() above."""
    form = request.form
    name = (form.get("name") or "").strip()
    submitter_name = (form.get("submitter_name") or "").strip()
    source_url = (form.get("source_url") or "").strip()
    if not name or not submitter_name:
        flash("Recipe name and your name are required.")
        return redirect(url_for("submit_form"))

    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    cuisine = form.get("cuisine") or ""
    if cuisine not in db.CUISINES:
        cuisine = ""

    dietary_tags = ",".join(t for t in form.getlist("dietary") if t in db.DIETARY_TAGS)
    prep_time = (form.get("prep_time") or "").strip()
    servings = (form.get("servings") or "").strip()
    ingredients = (form.get("ingredients") or "").strip()
    instructions = (form.get("instructions") or "").strip()
    story = (form.get("story") or "").strip()
    proofreading_notes = _run_proofreading(name, ingredients, instructions, story)

    conn = db.get_db()
    new_id = conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, cuisine, dietary_tags, prep_time, servings,
            ingredients, instructions, story, source_url, status, submitted_at,
            parse_model, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
           RETURNING id""",
        (
            name,
            submitter_name,
            category,
            cuisine,
            dietary_tags,
            prep_time,
            servings,
            ingredients,
            instructions,
            story,
            source_url,
            db.now_iso(),
            (form.get("parse_model") or "").strip() or None,
            proofreading_notes,
        ),
    ).fetchone()["id"]
    conn.commit()
    _notify_admin(
        f'New recipe pending review: "{name}"',
        f"{submitter_name} submitted \"{name}\" (from a web link) for review.\n\n"
        f"Review it: {url_for('admin_recipe_edit', recipe_id=new_id, _external=True)}",
    )
    return redirect(url_for("submitted"))


@app.route("/submitted")
def submitted():
    return render_template("submitted.html")


def _store_dish_photo(file_storage) -> str | None:
    raw_bytes = file_storage.read()
    filename = file_storage.filename
    if not image_utils.is_image(filename):
        return None
    try:
        jpeg_bytes = image_utils.normalize_image(raw_bytes)
    except image_utils.UnsupportedImageError as exc:
        app.logger.warning("Skipping dish photo, unreadable format: %s", exc)
        return None
    if not storage.storage_configured():
        return None
    try:
        return storage.upload_bytes(jpeg_bytes, "dish.jpg", "image/jpeg", "dishes")
    except RuntimeError as exc:
        app.logger.error("Supabase upload failed: %s", exc)
        return None


# -------------------------------------------------------------- admin -----

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            next_url = request.args.get("next") or url_for("admin_queue")
            return redirect(next_url)
        flash("Wrong password.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_root():
    return redirect(url_for("admin_queue"))


@app.route("/admin/queue")
@admin_required
def admin_queue():
    conn = db.get_db()
    pending = conn.execute(
        "SELECT * FROM recipes WHERE status = 'pending' ORDER BY submitted_at ASC"
    ).fetchall()
    return render_template("admin_queue.html", recipes=pending)


@app.route("/admin/published")
@admin_required
def admin_published():
    conn = db.get_db()
    recipes = conn.execute(
        "SELECT * FROM recipes WHERE status = 'published' ORDER BY LOWER(name) ASC"
    ).fetchall()
    return render_template("admin_published.html", recipes=recipes)


@app.route("/admin/recipe/<int:recipe_id>")
@admin_required
def admin_recipe_edit(recipe_id):
    conn = db.get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if not recipe:
        abort(404)
    active_dietary = set(recipe["dietary_tags"].split(",")) if recipe["dietary_tags"] else set()

    # Split here rather than in the template - Jinja has no clean way to split
    # on a newline literal.
    proofreading_notes_list = [
        note.strip() for note in (recipe["proofreading_notes"] or "").split("\n") if note.strip()
    ]

    return render_template(
        "admin_edit.html",
        recipe=recipe,
        proofreading_notes_list=proofreading_notes_list,
        categories=db.CATEGORIES,
        cuisines=db.CUISINES,
        dietary_options=db.DIETARY_TAGS,
        active_dietary=active_dietary,
    )


def _save_recipe_fields(conn, recipe_id, form):
    """Applies the edit-form fields to a recipe. Caller commits."""
    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    cuisine = form.get("cuisine") or ""
    if cuisine not in db.CUISINES:
        cuisine = ""

    dietary_tags = ",".join(t for t in form.getlist("dietary") if t in db.DIETARY_TAGS)

    conn.execute(
        """UPDATE recipes SET name=%s, submitter_name=%s, category=%s, cuisine=%s,
           dietary_tags=%s, prep_time=%s, servings=%s, ingredients=%s, instructions=%s,
           story=%s, proofreading_notes='' WHERE id=%s""",
        (
            (form.get("name") or "").strip(),
            (form.get("submitter_name") or "").strip(),
            category,
            cuisine,
            dietary_tags,
            (form.get("prep_time") or "").strip(),
            (form.get("servings") or "").strip(),
            (form.get("ingredients") or "").strip(),
            (form.get("instructions") or "").strip(),
            (form.get("story") or "").strip(),
            recipe_id,
        ),
    )


@app.route("/admin/recipe/<int:recipe_id>/update", methods=["POST"])
@admin_required
def admin_recipe_update(recipe_id):
    conn = db.get_db()
    _save_recipe_fields(conn, recipe_id, request.form)
    conn.commit()
    flash("Saved.")
    return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))


@app.route("/admin/recipe/<int:recipe_id>/approve", methods=["POST"])
@admin_required
def admin_recipe_approve(recipe_id):
    conn = db.get_db()
    _save_recipe_fields(conn, recipe_id, request.form)
    conn.execute(
        "UPDATE recipes SET status='published', reviewed_at=%s WHERE id=%s",
        (db.now_iso(), recipe_id),
    )
    conn.commit()
    flash("Published!")
    return redirect(url_for("admin_queue"))


@app.route("/admin/recipe/<int:recipe_id>/reject", methods=["POST"])
@admin_required
def admin_recipe_reject(recipe_id):
    conn = db.get_db()
    _save_recipe_fields(conn, recipe_id, request.form)
    conn.execute(
        "UPDATE recipes SET status='rejected', reviewed_at=%s WHERE id=%s",
        (db.now_iso(), recipe_id),
    )
    conn.commit()
    flash("Rejected.")
    return redirect(url_for("admin_queue"))


@app.route("/admin/recipe/<int:recipe_id>/reprocess", methods=["POST"])
@admin_required
def admin_recipe_reprocess(recipe_id):
    conn = db.get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if not recipe or not (recipe["source_image_path"] or recipe["source_url"]):
        flash("No source image or link to reprocess.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    try:
        if recipe["source_image_path"]:
            import requests

            # Handle multiple source images (newline-separated)
            image_paths = [p.strip() for p in recipe["source_image_path"].split("\n") if p.strip()]

            if len(image_paths) > 1:
                # Multiple images - use parse_recipe_multi
                files_with_types = []
                for path in image_paths:
                    resp = requests.get(path, timeout=30)
                    resp.raise_for_status()
                    media_type = "application/pdf" if path.lower().endswith(".pdf") else "image/jpeg"
                    files_with_types.append((resp.content, media_type))
                parsed = parse_recipe_multi(files_with_types, use_better_model=True)
            else:
                # Single image - use parse_recipe
                resp = requests.get(image_paths[0], timeout=30)
                resp.raise_for_status()
                media_type = "application/pdf" if image_paths[0].lower().endswith(".pdf") else "image/jpeg"
                parsed = parse_recipe(resp.content, media_type, use_better_model=True)
        else:
            parsed, _final_url = parse_recipe_from_url(recipe["source_url"], use_better_model=True)
    except (ParseError, FetchError) as exc:
        flash(f"Reprocessing failed: {exc}")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    # Never let a reprocess blank out a field that currently has content. A
    # story is usually typed by the submitter and isn't on the recipe card at
    # all, so a re-read of the photo legitimately returns nothing for it -
    # writing that empty string back would silently delete their words.
    conn.execute(
        """UPDATE recipes SET name=%s, category=%s, cuisine=%s, prep_time=%s, servings=%s,
           ingredients=%s, instructions=%s, story=%s, parse_model=%s WHERE id=%s""",
        (
            parsed["name"] or recipe["name"],
            parsed["category"],
            parsed.get("cuisine") or recipe["cuisine"],
            parsed.get("prep_time") or recipe["prep_time"],
            parsed.get("servings") or recipe["servings"],
            parsed["ingredients"] or recipe["ingredients"],
            parsed["instructions"] or recipe["instructions"],
            parsed["story"] or recipe["story"],
            parsed["parse_model"],
            recipe_id,
        ),
    )
    conn.commit()
    flash("Reprocessed with the better model.")
    return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))


@app.route("/admin/recipe/<int:recipe_id>/preview-fix", methods=["POST"])
@admin_required
def admin_recipe_preview_fix(recipe_id):
    """Preview a proposed fix before applying it.
    Admin clicks Fix button → See proposed change → Accept or Reject."""
    conn = db.get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if not recipe:
        abort(404)

    issue_text = request.form.get("issue", "").strip()
    if not issue_text:
        flash("No issue specified.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    # Ask Claude to propose a fix (don't apply yet)
    proposal = propose_recipe_fix(
        recipe["name"],
        recipe["ingredients"],
        recipe["instructions"],
        recipe["story"],
        issue_text,
    )

    if not proposal.get("success"):
        flash(f"Could not propose fix: {proposal.get('error', 'Unknown error')}")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    # Drop any option the accept step would refuse, so we never show an Accept
    # button for a change that can't be applied.
    safe_options = []
    for opt in proposal["options"]:
        if _reject_reason(proposal["original_snippet"], opt["fixed_snippet"]):
            app.logger.warning(
                "Discarded unsafe fix proposal for recipe %s: %s", recipe_id, opt
            )
            continue
        safe_options.append(opt)

    if not safe_options:
        flash("Claude's suggested fix didn't look safe to apply, so it was discarded. "
              "Nothing was changed - you can edit the field by hand.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))
    proposal["options"] = safe_options

    # Build the real before/after for the WHOLE field, so what's on screen is
    # exactly what would be written - a line-level diff hid the fact that the
    # whole column was being replaced.
    field_before = recipe[proposal["fixed_field"]] or ""
    for opt in proposal["options"]:
        opt["field_after"] = field_before.replace(
            proposal["original_snippet"], opt["fixed_snippet"]
        )
        opt["is_deletion"] = opt["fixed_snippet"].strip() == ""

    return render_template(
        "admin_fix_preview.html",
        recipe=recipe,
        issue=issue_text,
        issue_index=request.form.get("issue_index", ""),
        proposal=proposal,
        field_before=field_before,
    )


@app.route("/admin/recipe/<int:recipe_id>/accept-fix", methods=["POST"])
@admin_required
def admin_recipe_accept_fix(recipe_id):
    """Accept a proposed fix and apply it to the recipe."""
    conn = db.get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if not recipe:
        abort(404)

    issue_text = request.form.get("issue", "").strip()
    fixed_field = request.form.get("fixed_field", "").strip()
    original_snippet = request.form.get("original_snippet", "")
    fixed_snippet = request.form.get("chosen_fix", "")

    if not issue_text or not fixed_field or not original_snippet:
        flash("Missing fix details - nothing was changed.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    if fixed_field not in ("name", "ingredients", "instructions", "story"):
        flash("Invalid field specified - nothing was changed.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    # Splice the snippet into the stored text here rather than trusting the form
    # to carry a whole replacement field. Re-checking against the recipe as it
    # is right now also catches the case where it changed since the preview.
    field_before = recipe[fixed_field] or ""
    if original_snippet not in field_before:
        flash(
            "That text is no longer in the recipe, so nothing was changed. "
            "The recipe may have been edited since the preview - try the fix again."
        )
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    reason = _reject_reason(original_snippet, fixed_snippet)
    if reason:
        flash(f"That fix was blocked because {reason}. Nothing was changed - "
              "please edit the field by hand.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    field_after = field_before.replace(original_snippet, fixed_snippet)

    # Backstop against a fix that would gut the field. A real correction edits a
    # line or two; anything that removes most of the text is a bug, not a fix.
    # Deliberate deletions are small, so measure against the snippet's own size.
    lost = len(field_before) - len(field_after)
    if lost > len(original_snippet) + 20:
        flash(
            "That fix would have removed far more text than it should have, so it "
            "was blocked. Nothing was changed - please edit the field by hand."
        )
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    # Drop the issue we just handled so the list shrinks as you work through
    # it - the other issues stay put, still each with their own decision.
    remaining_notes = _drop_issue(
        recipe["proofreading_notes"], issue_text, request.form.get("issue_index", "")
    )

    # Column name is from the fixed whitelist above, never raw user input.
    conn.execute(
        f"UPDATE recipes SET {fixed_field}=%s, proofreading_notes=%s WHERE id=%s",
        (field_after, remaining_notes, recipe_id),
    )
    conn.commit()

    shown = original_snippet if len(original_snippet) <= 60 else original_snippet[:60] + "…"
    flash(f"✓ Applied fix to {fixed_field}: '{shown}'")

    return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))


@app.route("/admin/recipe/<int:recipe_id>/reject-fix", methods=["POST"])
@admin_required
def admin_recipe_reject_fix(recipe_id):
    """Reject a proposed fix and return to recipe editing."""
    flash("Fix rejected. You can manually edit the recipe if needed.")
    return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))


@app.route("/admin/recipe/<int:recipe_id>/check-formatting", methods=["POST"])
@admin_required
def admin_recipe_check_formatting(recipe_id):
    """Run proofreading check on any recipe (published or pending).
    Generates a fresh list of formatting issues for Accept/Reject workflow.
    Admin can send published recipes back through review process."""
    conn = db.get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if not recipe:
        abort(404)

    # _run_proofreading already returns the newline-joined string we store -
    # joining it again would split it between every character.
    proofreading_notes = _run_proofreading(
        recipe["name"],
        recipe["ingredients"],
        recipe["instructions"],
        recipe["story"],
    )

    # Store the new proofreading notes (overwriting any old ones)
    conn.execute(
        "UPDATE recipes SET proofreading_notes=%s WHERE id=%s",
        (proofreading_notes, recipe_id),
    )
    conn.commit()

    issue_count = len([n for n in proofreading_notes.split("\n") if n.strip()])
    if issue_count:
        flash(f"Found {issue_count} formatting issue(s). Review and fix them below.")
    else:
        flash("✓ No formatting issues found! Recipe looks good.")

    return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))


@app.route("/admin/review/<int:review_id>/delete", methods=["POST"])
@admin_required
def admin_review_delete(review_id):
    conn = db.get_db()
    review = conn.execute("SELECT recipe_id FROM reviews WHERE id = %s", (review_id,)).fetchone()
    if not review:
        abort(404)
    conn.execute("DELETE FROM reviews WHERE id = %s", (review_id,))
    conn.commit()
    flash("Review deleted.")
    return redirect(url_for("recipe_detail", recipe_id=review["recipe_id"]) + "#reviews")


@app.route("/admin/corrections")
@admin_required
def admin_corrections():
    conn = db.get_db()
    pending = conn.execute(
        """SELECT e.*, r.name AS recipe_name FROM recipe_edits e
           JOIN recipes r ON r.id = e.recipe_id
           WHERE e.status = 'pending' ORDER BY e.submitted_at ASC"""
    ).fetchall()
    return render_template("admin_corrections.html", corrections=pending)


@app.route("/admin/correction/<int:edit_id>")
@admin_required
def admin_correction_detail(edit_id):
    conn = db.get_db()
    edit = conn.execute("SELECT * FROM recipe_edits WHERE id = %s", (edit_id,)).fetchone()
    if not edit:
        abort(404)
    recipe = conn.execute("SELECT * FROM recipes WHERE id = %s", (edit["recipe_id"],)).fetchone()
    if not recipe:
        abort(404)

    proposed = json.loads(edit["proposed"] or "{}")
    base = json.loads(edit["base"] or "{}")
    return render_template(
        "admin_correction_detail.html",
        edit=edit,
        recipe=recipe,
        field_diffs=_build_field_diffs(recipe, proposed, base),
    )


@app.route("/admin/correction/<int:edit_id>/approve", methods=["POST"])
@admin_required
def admin_correction_approve(edit_id):
    """Applies a suggested correction to the live recipe.

    Deliberately not routed through _save_recipe_fields: that writes
    submitter_name, forces status, and blanks proofreading_notes - and anything
    added to it later would silently join this path too.
    """
    conn = db.get_db()
    edit = conn.execute("SELECT * FROM recipe_edits WHERE id = %s", (edit_id,)).fetchone()
    if not edit:
        abort(404)

    # Claim the suggestion before touching the recipe, so a double-click (or a
    # replayed POST) finds it already handled and writes nothing.
    claimed = conn.execute(
        "UPDATE recipe_edits SET status='approved', reviewed_at=%s "
        "WHERE id=%s AND status='pending'",
        (db.now_iso(), edit_id),
    )
    if claimed.rowcount != 1:
        conn.rollback()
        flash("That suggestion had already been handled - nothing was changed.")
        return redirect(url_for("admin_corrections"))

    proposed = json.loads(edit["proposed"] or "{}")
    # Column names come from SUGGESTABLE_FIELDS, never from the stored JSON keys.
    cols = [c for c in SUGGESTABLE_FIELDS if c in proposed]
    if cols:
        assignments = ", ".join(f"{c} = %s" for c in cols)
        applied = conn.execute(
            f"UPDATE recipes SET {assignments} WHERE id = %s AND status = 'published'",
            [proposed[c] for c in cols] + [edit["recipe_id"]],
        )
        if applied.rowcount != 1:
            conn.rollback()
            flash("That recipe is no longer published - nothing was changed.")
            return redirect(url_for("admin_corrections"))
    conn.commit()

    changed = ", ".join(FIELD_LABELS[c].lower() for c in cols) or "nothing"
    flash(f"✓ Applied {edit['suggester_name']}'s correction ({changed}).")
    return redirect(url_for("admin_corrections"))


@app.route("/admin/correction/<int:edit_id>/reject", methods=["POST"])
@admin_required
def admin_correction_reject(edit_id):
    conn = db.get_db()
    rejected = conn.execute(
        "UPDATE recipe_edits SET status='rejected', reviewed_at=%s "
        "WHERE id=%s AND status='pending'",
        (db.now_iso(), edit_id),
    )
    if rejected.rowcount != 1:
        conn.rollback()
        flash("That suggestion had already been handled.")
        return redirect(url_for("admin_corrections"))
    conn.commit()
    flash("Suggestion rejected. The recipe is unchanged.")
    return redirect(url_for("admin_corrections"))


@app.route("/admin/photos")
@admin_required
def admin_photos():
    conn = db.get_db()
    pending = conn.execute(
        """SELECT dp.*, r.name AS recipe_name FROM dish_photos dp
           JOIN recipes r ON r.id = dp.recipe_id
           WHERE dp.status = 'pending' ORDER BY dp.submitted_at ASC"""
    ).fetchall()
    return render_template("admin_photos.html", photos=pending)


@app.route("/admin/photo/<int:photo_id>/approve", methods=["POST"])
@admin_required
def admin_photo_approve(photo_id):
    conn = db.get_db()
    photo = conn.execute("SELECT id FROM dish_photos WHERE id = %s", (photo_id,)).fetchone()
    if not photo:
        abort(404)
    conn.execute(
        "UPDATE dish_photos SET status='published', reviewed_at=%s WHERE id=%s",
        (db.now_iso(), photo_id),
    )
    conn.commit()
    flash("Photo published.")
    return redirect(url_for("admin_photos"))


@app.route("/admin/photo/<int:photo_id>/reject", methods=["POST"])
@admin_required
def admin_photo_reject(photo_id):
    conn = db.get_db()
    photo = conn.execute("SELECT id FROM dish_photos WHERE id = %s", (photo_id,)).fetchone()
    if not photo:
        abort(404)
    conn.execute(
        "UPDATE dish_photos SET status='rejected', reviewed_at=%s WHERE id=%s",
        (db.now_iso(), photo_id),
    )
    conn.commit()
    flash("Photo rejected.")
    return redirect(url_for("admin_photos"))


@app.route("/admin/photo/<int:photo_id>/delete", methods=["POST"])
@admin_required
def admin_photo_delete(photo_id):
    """Removes an already-published photo (post-hoc moderation)."""
    conn = db.get_db()
    photo = conn.execute("SELECT recipe_id FROM dish_photos WHERE id = %s", (photo_id,)).fetchone()
    if not photo:
        abort(404)
    conn.execute("DELETE FROM dish_photos WHERE id = %s", (photo_id,))
    conn.commit()
    flash("Photo removed.")
    return redirect(url_for("recipe_detail", recipe_id=photo["recipe_id"]) + "#photos")


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        db.set_setting("intro_text", (request.form.get("intro_text") or "").strip())
        flash("Homepage welcome message saved.")
        return redirect(url_for("admin_settings"))

    intro_text = db.get_setting("intro_text", db.DEFAULT_INTRO_TEXT)
    return render_template("admin_settings.html", intro_text=intro_text)


@app.errorhandler(413)
def too_large(_exc):
    flash("That file is too large (15 MB max). Try a smaller photo.")
    return redirect(url_for("submit_form"))


@app.errorhandler(psycopg.OperationalError)
def db_unavailable(exc):
    """The database is unreachable - most likely Supabase's free-tier project
    waking up from a pause, or a brief network hiccup. Show a friendly
    self-refreshing page instead of a raw 500 error."""
    app.logger.warning("Database temporarily unavailable: %s", exc)
    return render_template("db_warming_up.html"), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
