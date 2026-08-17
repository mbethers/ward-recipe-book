"""University Ward Recipe Book - Flask app.

Public: browse/search published recipes, submit a recipe (typed or photo/PDF upload).
Admin (/admin, password-gated): review pending submissions, edit, approve/reject,
manage theme tags.
"""
import functools
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # must run before importing db/storage, which read env vars at import time
except ImportError:
    pass

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import db
from ai_parse import ParseError, parse_recipe
import image_utils
import storage

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

def get_themes():
    conn = db.get_db()
    return conn.execute("SELECT * FROM themes ORDER BY is_current DESC, name ASC").fetchall()


def ensure_theme(name: str):
    """Make sure a theme tag exists in the themes table (used for free-typed new themes)."""
    name = (name or "").strip()
    if not name:
        return
    conn = db.get_db()
    conn.execute(
        """INSERT INTO themes (name, is_current, created_at) VALUES (%s, FALSE, %s)
           ON CONFLICT (name) DO NOTHING""",
        (name, db.now_iso()),
    )
    conn.commit()


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"ward_name": WARD_NAME}


# ------------------------------------------------------------- public UI --

@app.route("/")
def index():
    conn = db.get_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    theme = request.args.get("theme", "").strip()

    sql = """SELECT r.*,
                    COALESCE(AVG(rv.rating), 0)::float AS avg_rating,
                    COUNT(rv.id) AS review_count
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
    if theme:
        sql += " AND r.theme_tag = %s"
        params.append(theme)
    sql += " GROUP BY r.id ORDER BY LOWER(r.name) ASC"

    recipes = conn.execute(sql, params).fetchall()
    themes = get_themes()
    return render_template(
        "index.html",
        recipes=recipes,
        categories=db.CATEGORIES,
        themes=themes,
        q=q,
        active_category=category,
        active_theme=theme,
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
    return render_template("recipe.html", recipe=recipe, reviews=reviews, avg_rating=avg_rating)


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


@app.route("/submit", methods=["GET"])
def submit_form():
    themes = get_themes()
    return render_template("submit.html", categories=db.CATEGORIES, themes=themes)


@app.route("/submit/text", methods=["POST"])
def submit_text():
    form = request.form
    theme_tag = (form.get("new_theme") or "").strip() or (form.get("theme_tag") or "").strip()
    if not theme_tag:
        flash("Please choose or enter a theme.")
        return redirect(url_for("submit_form"))
    ensure_theme(theme_tag)

    name = (form.get("name") or "").strip()
    submitter_name = (form.get("submitter_name") or "").strip()
    if not name or not submitter_name:
        flash("Recipe name and your name are required.")
        return redirect(url_for("submit_form"))

    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    photo_url = None
    photo = request.files.get("photo")
    if photo and photo.filename:
        photo_url = _store_dish_photo(photo)

    conn = db.get_db()
    conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, theme_tag, ingredients, instructions,
            story, photo_path, status, submitted_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
        (
            name,
            submitter_name,
            category,
            theme_tag,
            (form.get("ingredients") or "").strip(),
            (form.get("instructions") or "").strip(),
            (form.get("story") or "").strip(),
            photo_url,
            db.now_iso(),
        ),
    )
    conn.commit()
    return redirect(url_for("submitted"))


@app.route("/submit/photo", methods=["POST"])
def submit_photo():
    form = request.form
    theme_tag = (form.get("new_theme") or "").strip() or (form.get("theme_tag") or "").strip()
    submitter_name = (form.get("submitter_name") or "").strip()
    upload = request.files.get("source_file")

    if not theme_tag or not submitter_name or not upload or not upload.filename:
        flash("Your name, a theme, and a photo or PDF are all required.")
        return redirect(url_for("submit_form"))
    ensure_theme(theme_tag)

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

    conn = db.get_db()
    conn.execute(
        """INSERT INTO recipes
           (name, submitter_name, category, theme_tag, ingredients, instructions,
            story, source_image_path, status, submitted_at, parse_model)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)""",
        (
            parsed["name"] or "(untitled - needs review)",
            submitter_name,
            parsed["category"],
            theme_tag,
            parsed["ingredients"],
            parsed["instructions"],
            parsed["story"],
            source_url,
            db.now_iso(),
            parsed.get("parse_model"),
        ),
    )
    conn.commit()

    if parse_error:
        flash("Thanks! We saved your upload, but the automatic reading had trouble - an admin will fill in the details by hand.")
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


# --------------------------------------------------------------- api ------

@app.route("/api/themes")
def api_themes():
    themes = get_themes()
    return {"themes": [t["name"] for t in themes]}


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
    themes = get_themes()
    return render_template("admin_edit.html", recipe=recipe, categories=db.CATEGORIES, themes=themes)


def _save_recipe_fields(conn, recipe_id, form):
    """Applies the edit-form fields to a recipe. Caller commits."""
    theme_tag = (form.get("new_theme") or "").strip() or (form.get("theme_tag") or "").strip()
    ensure_theme(theme_tag)
    category = form.get("category") or "Other"
    if category not in db.CATEGORIES:
        category = "Other"

    conn.execute(
        """UPDATE recipes SET name=%s, submitter_name=%s, category=%s, theme_tag=%s,
           ingredients=%s, instructions=%s, story=%s WHERE id=%s""",
        (
            (form.get("name") or "").strip(),
            (form.get("submitter_name") or "").strip(),
            category,
            theme_tag,
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
    if not recipe or not recipe["source_image_path"]:
        flash("No source image to reprocess.")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    import requests

    resp = requests.get(recipe["source_image_path"], timeout=30)
    resp.raise_for_status()
    media_type = "application/pdf" if recipe["source_image_path"].lower().endswith(".pdf") else "image/jpeg"

    try:
        parsed = parse_recipe(resp.content, media_type, use_better_model=True)
    except ParseError as exc:
        flash(f"Reprocessing failed: {exc}")
        return redirect(url_for("admin_recipe_edit", recipe_id=recipe_id))

    conn.execute(
        """UPDATE recipes SET name=%s, category=%s, ingredients=%s, instructions=%s,
           story=%s, parse_model=%s WHERE id=%s""",
        (
            parsed["name"] or recipe["name"],
            parsed["category"],
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


@app.route("/admin/themes", methods=["GET", "POST"])
@admin_required
def admin_themes():
    conn = db.get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = (request.form.get("name") or "").strip()
            if name:
                ensure_theme(name)
        elif action == "set_current":
            theme_id = request.form.get("theme_id")
            conn.execute("UPDATE themes SET is_current = FALSE")
            conn.execute("UPDATE themes SET is_current = TRUE WHERE id = %s", (theme_id,))
            conn.commit()
        elif action == "delete":
            theme_id = request.form.get("theme_id")
            theme = conn.execute("SELECT * FROM themes WHERE id=%s", (theme_id,)).fetchone()
            if theme:
                in_use = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM recipes WHERE theme_tag = %s", (theme["name"],)
                ).fetchone()["cnt"]
                if in_use:
                    flash(f'"{theme["name"]}" is used by {in_use} recipe(s) and can\'t be deleted.')
                else:
                    conn.execute("DELETE FROM themes WHERE id=%s", (theme_id,))
                    conn.commit()
        return redirect(url_for("admin_themes"))

    themes = get_themes()
    return render_template("admin_themes.html", themes=themes)


@app.errorhandler(413)
def too_large(_exc):
    flash("That file is too large (15 MB max). Try a smaller photo.")
    return redirect(url_for("submit_form"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
