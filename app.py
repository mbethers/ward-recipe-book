"""University Ward Cookbook - Flask app.

Public: browse/search published recipes, submit a recipe (typed or photo/PDF upload).
Admin (/admin, password-gated): review pending submissions, edit, approve/reject.
"""
import functools
import os
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
from ai_parse import ParseError, parse_recipe, proofread_recipe
import image_utils
import email_utils
import storage
from web_recipe import FetchError, parse_recipe_from_url

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-not-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB
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
           (name, submitter_name, category, cuisine, dietary_tags, ingredients,
            instructions, story, photo_path, status, submitted_at, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
           RETURNING id""",
        (
            name,
            submitter_name,
            category,
            cuisine,
            dietary_tags,
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
    upload = request.files.get("source_file")

    if not submitter_name or not upload or not upload.filename:
        flash("Your name and a photo or PDF are both required.")
        return redirect(url_for("submit_form"))

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
            flash(str(exc))
            return redirect(url_for("submit_form"))
        store_bytes, store_content_type, store_ext = parse_bytes, "image/jpeg", ".jpg"
    else:
        flash("Please upload a photo (jpg/png/heic) or a PDF.")
        return redirect(url_for("submit_form"))

    source_url = None
    if storage.storage_configured():
        try:
            source_url = storage.upload_bytes(store_bytes, f"source{store_ext}", store_content_type, "sources")
        except RuntimeError as exc:
            app.logger.error("Supabase upload failed: %s", exc)

    parsed = {
        "name": "",
        "category": "Other",
        "cuisine": "",
        "ingredients": "",
        "instructions": "",
        "story": "",
        "parse_model": None,
    }
    parse_error = None
    try:
        parsed = parse_recipe(parse_bytes, media_type)
    except ParseError as exc:
        parse_error = str(exc)
        app.logger.error("Claude parse failed: %s", exc)

    proofreading_notes = ""
    if not parse_error:
        proofreading_notes = _run_proofreading(
            parsed["name"], parsed["ingredients"], parsed["instructions"], parsed["story"]
        )

    conn = db.get_db()
    display_name = parsed["name"] or "(untitled - needs review)"
    new_id = conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, cuisine, ingredients, instructions,
            story, source_image_path, status, submitted_at, parse_model, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
           RETURNING id""",
        (
            display_name,
            submitter_name,
            parsed["category"],
            parsed.get("cuisine", ""),
            parsed["ingredients"],
            parsed["instructions"],
            parsed["story"],
            source_url,
            db.now_iso(),
            parsed.get("parse_model"),
            proofreading_notes,
        ),
    ).fetchone()["id"]
    conn.commit()
    _notify_admin(
        f'New recipe pending review: "{display_name}"',
        f"{submitter_name} submitted \"{display_name}\" (from a photo/PDF upload) for review.\n\n"
        f"Review it: {url_for('admin_recipe_edit', recipe_id=new_id, _external=True)}",
    )

    if parse_error:
        flash("Thanks! We saved your upload, but the automatic reading had trouble - an admin will fill in the details by hand.")
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
        # We reached the page but couldn't make a recipe out of it - still
        # worth saving so an admin can look at the link and fill it in by hand,
        # same as a photo/PDF that failed to read.
        app.logger.error("Web recipe parse failed for %s: %s", recipe_url, exc)
        parsed = {"name": "", "category": "Other", "cuisine": "", "ingredients": "",
                  "instructions": "", "story": "", "parse_model": None}
        final_url = recipe_url

    proofreading_notes = ""
    if parsed["name"]:
        proofreading_notes = _run_proofreading(
            parsed["name"], parsed["ingredients"], parsed["instructions"], parsed["story"]
        )

    conn = db.get_db()
    display_name = parsed["name"] or "(untitled - needs review)"
    new_id = conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, cuisine, ingredients, instructions,
            story, source_url, status, submitted_at, parse_model, proofreading_notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
           RETURNING id""",
        (
            display_name,
            submitter_name,
            parsed["category"],
            parsed.get("cuisine", ""),
            parsed["ingredients"],
            parsed["instructions"],
            parsed["story"],
            final_url,
            db.now_iso(),
            parsed.get("parse_model"),
            proofreading_notes,
        ),
    ).fetchone()["id"]
    conn.commit()
    _notify_admin(
        f'New recipe pending review: "{display_name}"',
        f"{submitter_name} submitted \"{display_name}\" (from a web link) for review.\n\n"
        f"Review it: {url_for('admin_recipe_edit', recipe_id=new_id, _external=True)}",
    )

    if not parsed["name"]:
        flash("Thanks! We saved your link, but the automatic reading had trouble - an admin will fill in the details by hand.")
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
    return render_template(
        "admin_edit.html",
        recipe=recipe,
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
           dietary_tags=%s, ingredients=%s, instructions=%s, story=%s,
           proofreading_notes='' WHERE id=%s""",
        (
            (form.get("name") or "").strip(),
            (form.get("submitter_name") or "").strip(),
            category,
            cuisine,
            dietary_tags,
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

            resp = requests.get(recipe["source_image_path"], timeout=30)
            resp.raise_for_status()
            media_type = "application/pdf" if recipe["source_image_path"].lower().endswith(".pdf") else "image/jpeg"
            parsed = parse_recipe(resp.content, media_type, use_better_model=True)
        else:
            parsed, _final_url = parse_recipe_from_url(recipe["source_url"], use_better_model=True)
    except (ParseError, FetchError) as exc:
        flash(f"Reprocessing failed: {exc}")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    conn.execute(
        """UPDATE recipes SET name=%s, category=%s, cuisine=%s, ingredients=%s,
           instructions=%s, story=%s, parse_model=%s WHERE id=%s""",
        (
            parsed["name"] or recipe["name"],
            parsed["category"],
            parsed.get("cuisine") or recipe["cuisine"],
            parsed["ingredients"],
            parsed["instructions"],
            parsed["story"],
            parsed["parse_model"],
            recipe_id,
        ),
    )
    conn.commit()
    flash("Reprocessed with the better model.")
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
