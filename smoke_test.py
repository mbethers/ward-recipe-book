"""Drive the admin routes against a stub database.

Catches the class of bug that repeatedly reached production here: Jinja
constructs that only fail at render time, forms that submit to the wrong
place, and fix proposals that overwrite a whole field.

    ./venv/bin/python3 smoke_test.py
"""
import html
import json
import os
import re
import sys

os.environ["DATABASE_URL"] = ""          # makes init_db() a no-op
os.environ["ADMIN_PASSWORD"] = "testpass"
os.environ["FLASK_SECRET_KEY"] = "test-key"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as flaskapp  # noqa: E402
import db  # noqa: E402

INGREDIENTS = "1 T. butter\n1/4 cup sugar\n1 8oz. cream cheese\n2 cups flour"
ISSUES = [
    'Measurement abbreviations: "T." should be "Tbsp."',
    'Sugar line needs checking',
    '"1 8oz. cream cheese" should be "8 oz. cream cheese"',
]
# snippet -> replacement, one per issue, each present in INGREDIENTS
FIXES = [
    ("1 T. butter", "1 Tbsp. butter"),
    ("1/4 cup sugar", "1/4 cup white sugar"),
    ("1 8oz. cream cheese", "8 oz. cream cheese"),
]
BASE = {
    "id": 7, "name": "Blueberry Crumble Pie", "submitter_name": "Kaylyn Betancourt",
    "category": "Dessert", "cuisine": "American", "dietary_tags": "",
    "prep_time": "1 hr", "servings": "8",
    "ingredients": INGREDIENTS, "instructions": "1. Preheat oven to 350°F.",
    "story": "", "photo_path": None,
    "source_image_path": "https://example.test/s.jpg", "source_url": "",
    "status": "published", "submitted_at": "x", "reviewed_at": None,
    "parse_model": "claude-sonnet-5", "proofreading_notes": "\n".join(ISSUES),
}
STATE = dict(BASE)


# A suggested correction, as recipe_edits would hold it.
EDIT = {
    "id": 1, "recipe_id": 7, "suggester_name": "Danielle", "note": "sugar amount is wrong",
    "proposed": "{}", "base": "{}", "status": "pending",
    "submitted_at": "2026-08-18T09:00:00+00:00", "reviewed_at": None,
    "recipe_name": "Blueberry Crumble Pie",
}
EDIT_STATE = dict(EDIT)
INSERTED = []          # rows an INSERT would have created


class Result:
    def __init__(self, rows, rowcount=1):
        self.rows = rows
        self.rowcount = rowcount

    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Conn:
    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()

        if s.startswith("update recipe_edits"):
            # Honour the "WHERE status='pending'" claim so double-approve is testable.
            if "status='pending'" in s.replace(" ", "") and EDIT_STATE["status"] != "pending":
                return Result([], rowcount=0)
            for col, val in zip(re.findall(r"(\w+)\s*=\s*%s", sql), params or []):
                EDIT_STATE[col] = val
            if "status='approved'" in s.replace(" ", ""):
                EDIT_STATE["status"] = "approved"
            elif "status='rejected'" in s.replace(" ", ""):
                EDIT_STATE["status"] = "rejected"
            return Result([], rowcount=1)

        if s.startswith("update recipes"):
            if "status = 'published'" in s and STATE.get("status") != "published":
                return Result([], rowcount=0)
            for col, val in zip(re.findall(r"(\w+)\s*=\s*%s", sql), params or []):
                STATE[col] = val
            return Result([], rowcount=1)

        if s.startswith("insert into recipe_edits"):
            INSERTED.append(params)
            return Result([{"id": 1}])
        if s.startswith(("insert", "delete", "update")):
            return Result([{"id": 7}])

        if "count(*)" in s and "recipe_edits" in s:
            return Result([{"n": 0}])
        if "from recipe_edits" in s:
            return Result([dict(EDIT_STATE)])
        if "from recipes" in s:
            # Honour a published-only filter, so the 404 guards are testable.
            if "status = 'published'" in s and STATE.get("status") != "published":
                return Result([])
            return Result([dict(STATE)])
        if "from settings" in s:
            return Result([{"value": "intro"}])
        return Result([])

    def commit(self): pass
    def rollback(self): pass


db.get_db = lambda: Conn()
flaskapp.db.get_db = lambda: Conn()
client = flaskapp.app.test_client()
with client.session_transaction() as sess:
    sess["is_admin"] = True

fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"\n         {detail}" if not cond else ""))
    if not cond:
        fails.append(label)


def listed():
    body = client.get("/admin/recipe/7").get_data(as_text=True)
    return [html.unescape(x) for x in re.findall(r'class="issue-text">([^<]*)<', body)]


def reset():
    STATE.clear()
    STATE.update(BASE)
    EDIT_STATE.clear()
    EDIT_STATE.update(EDIT)
    INSERTED.clear()


def set_edit(proposed, base=None):
    """Load a stored suggestion into the stub."""
    EDIT_STATE["proposed"] = json.dumps(proposed)
    EDIT_STATE["base"] = json.dumps(base if base is not None else
                                    {k: BASE[k] for k in proposed})


def recipe_snapshot():
    return {k: STATE[k] for k in BASE}


# --------------------------------------------------------------- recipe page
print("Recipe page:\n")
reset()
r = client.get("/admin/recipe/7")
check("edit page 200", r.status_code == 200, str(r.status_code))
check("3 full-text issue rows", listed() == ISSUES, str(listed()))
check("no single-character rows", all(len(x) > 1 for x in listed()))

# -------------------------------------------------- fix preview, 1 & 2 opts
print("\nFix preview renders for one and for two options:\n")
for n in (1, 2):
    flaskapp.propose_recipe_fix = lambda *a, _n=n, **k: {
        "success": True, "fixed_field": "ingredients",
        "original_snippet": "1 T. butter",
        "options": [{"fixed_snippet": f"opt{i} butter", "explanation": "e"} for i in range(_n)],
    }
    resp = client.post("/admin/recipe/7/preview-fix",
                       data={"issue": ISSUES[0], "issue_index": "0"})
    body = resp.get_data(as_text=True)
    card = body[body.find("fix-preview-container"):]
    check(f"{n} option(s): 200", resp.status_code == 200, str(resp.status_code))
    check(f"{n} option(s): exactly one <form>", card.count("<form") == 1, str(card.count("<form")))
    m = re.search(r'formaction="([^"]+)"', card)
    check(f"{n} option(s): Reject posts to reject-fix",
          m is not None and m.group(1).endswith("/reject-fix"), m.group(1) if m else "missing")
    check(f"{n} option(s): whole field shown", "2 cups flour" in card)

# ------------------------------------------------- accept walks the list down
print("\nAccepting each fix in turn:\n")
reset()
for i, (snippet, replacement) in enumerate(FIXES):
    before = listed()
    client.post("/admin/recipe/7/accept-fix", data={
        "issue": ISSUES[i], "issue_index": "0", "fixed_field": "ingredients",
        "original_snippet": snippet, "chosen_fix": replacement})
    after = listed()
    check(f"round {i+1}: one fewer row ({len(before)} -> {len(after)})",
          len(after) == len(before) - 1, str(after))
    check(f"round {i+1}: resolved issue gone", ISSUES[i] not in after)
    check(f"round {i+1}: replacement present", replacement in STATE["ingredients"],
          repr(STATE["ingredients"]))
    check(f"round {i+1}: all 4 ingredient lines intact",
          len(STATE["ingredients"].splitlines()) == 4, repr(STATE["ingredients"]))

check("list empty at the end", listed() == [], str(listed()))

# --------------------------------------------------------- destructive shapes
print("\nDestructive fixes are refused:\n")
reset()
client.post("/admin/recipe/7/accept-fix", data={
    "issue": ISSUES[0], "issue_index": "0", "fixed_field": "ingredients",
    "original_snippet": "text that is not in the recipe", "chosen_fix": "8 oz. sour cream"})
check("snippet absent -> nothing written", STATE["ingredients"] == INGREDIENTS,
      repr(STATE["ingredients"]))

reset()
client.post("/admin/recipe/7/accept-fix", data={
    "issue": ISSUES[0], "issue_index": "0", "fixed_field": "ingredients",
    "original_snippet": INGREDIENTS,
    "chosen_fix": "Remove the duplicate fragments entirely, keeping only the core instruction."})
check("editorial prose -> nothing written", STATE["ingredients"] == INGREDIENTS,
      repr(STATE["ingredients"]))

reset()
client.post("/admin/recipe/7/accept-fix", data={
    "issue": ISSUES[0], "issue_index": "0", "fixed_field": "ingredients",
    "original_snippet": INGREDIENTS, "chosen_fix": "8 oz. low fat sour cream"})
check("4 lines collapsed to 1 -> nothing written", STATE["ingredients"] == INGREDIENTS,
      repr(STATE["ingredients"]))

# ---------------------------------------------------------------------- misc
print("\nReject, and the check-formatting round trip:\n")
reset()
client.post("/admin/recipe/7/reject-fix")
check("reject leaves all 3 issues", len(listed()) == 3, str(len(listed())))
check("reject changes nothing", STATE["ingredients"] == INGREDIENTS)

flaskapp._run_proofreading = lambda *a, **k: "fresh one\nfresh two"
resp = client.post("/admin/recipe/7/check-formatting")
check("check-formatting redirects", resp.status_code == 302, str(resp.status_code))
check("notes stored intact, not character-split",
      STATE["proofreading_notes"] == "fresh one\nfresh two",
      repr(STATE["proofreading_notes"])[:80])
check("renders 2 rows", listed() == ["fresh one", "fresh two"], str(listed()))

print("\nOther admin pages:\n")
for path in ("/admin/queue", "/admin/published", "/admin/corrections"):
    check(f"{path} 200", client.get(path).status_code == 200)

# ===================================================== suggested corrections ==
# The invariant under test: a published recipe cannot change without an admin
# clicking approve.

print("\nSuggest form:\n")
reset()
r = client.get("/recipe/7/suggest")
body = r.get_data(as_text=True)
check("suggest form 200", r.status_code == 200, str(r.status_code))
check("prefilled with the live ingredients", "1 T. butter" in body)
check("attribution is NOT editable", 'name="submitter_name"' not in body)
check("honeypot present", 'name="website"' in body)

reset()
STATE["status"] = "pending"
check("suggest on an unpublished recipe -> 404 (GET)",
      client.get("/recipe/7/suggest").status_code == 404)
check("suggest on an unpublished recipe -> 404 (POST)",
      client.post("/recipe/7/suggest", data={"suggester_name": "X", "name": "Y"}).status_code == 404)

print("\nSuggesting never touches the published recipe:\n")
reset()
before = recipe_snapshot()
client.post("/recipe/7/suggest", data={
    "suggester_name": "Danielle", "note": "lots of fixes",
    "name": "Totally Different Pie", "category": "Main", "cuisine": "Italian",
    "dietary": ["Vegan"], "prep_time": "9 hr", "servings": "99",
    "ingredients": "nothing but sand", "instructions": "do not bake",
    "story": "rewritten"})
check("recipe row completely unchanged", recipe_snapshot() == before, str(recipe_snapshot()))
check("a suggestion row was inserted", len(INSERTED) == 1, str(len(INSERTED)))
check("public page still shows the original", "1 T. butter" in client.get("/recipe/7").get_data(as_text=True))

print("\nValidation:\n")
reset()
crlf = INGREDIENTS.replace("\n", "\r\n")
client.post("/recipe/7/suggest", data={"suggester_name": "D", "ingredients": crlf})
check("CRLF no-op refused, nothing inserted", INSERTED == [], str(INSERTED))

reset()
client.post("/recipe/7/suggest", data={"suggester_name": "D", "name": "x" * 500})
check("over-cap field refused, nothing inserted", INSERTED == [], str(INSERTED))
check("  ...and the recipe is untouched", recipe_snapshot() == {k: BASE[k] for k in BASE})

reset()
client.post("/recipe/7/suggest", data={"suggester_name": "", "name": "New Name"})
check("missing suggester name refused", INSERTED == [], str(INSERTED))

reset()
client.post("/recipe/7/suggest", data={
    "suggester_name": "Bot", "website": "http://spam", "name": "Spam Name"})
check("honeypot submission discarded", INSERTED == [], str(INSERTED))

print("\nAdmin review screen:\n")
reset()
set_edit({"ingredients": "1 Tbsp. butter\n1/4 cup sugar\n1 8oz. cream cheese\n2 cups flour"})
r = client.get("/admin/correction/1")
body = r.get_data(as_text=True)
check("detail 200", r.status_code == 200, str(r.status_code))
check("shows the removed line", "1 T. butter" in body)
check("shows the added line", "1 Tbsp. butter" in body)
check("shows the whole field before/after", "2 cups flour" in body)
check("single form, Reject via formaction", body.count('<form method="post"') == 1 and "/reject" in body)

print("\nApprove applies exactly the proposed columns:\n")
reset()
set_edit({"ingredients": "1 Tbsp. butter\n1/4 cup sugar\n1 8oz. cream cheese\n2 cups flour"})
client.post("/admin/correction/1/approve")
check("ingredients applied", STATE["ingredients"].startswith("1 Tbsp. butter"), repr(STATE["ingredients"]))
for col in ("submitter_name", "status", "reviewed_at", "proofreading_notes", "name", "story"):
    check(f"  {col} untouched", STATE[col] == BASE[col], f"{STATE[col]!r} != {BASE[col]!r}")
check("edit marked approved", EDIT_STATE["status"] == "approved", EDIT_STATE["status"])

print("\nAttribution cannot be hijacked:\n")
reset()
before = recipe_snapshot()
client.post("/recipe/7/suggest", data={
    "suggester_name": "Sneaky", "submitter_name": "Hacker", "name": "New Title"})
check("submitter_name absent from the stored proposal",
      INSERTED and "submitter_name" not in json.loads(INSERTED[0][3]), str(INSERTED))
reset()
set_edit({"name": "New Title"})
client.post("/admin/correction/1/approve")
check("after approve, credit is unchanged", STATE["submitter_name"] == BASE["submitter_name"],
      STATE["submitter_name"])

print("\nDouble-approve and reject:\n")
reset()
set_edit({"ingredients": "first approved value\nsecond line\nthird line\nfourth line"})
client.post("/admin/correction/1/approve")
first = STATE["ingredients"]
set_edit({"ingredients": "SHOULD NOT BE APPLIED\nx\ny\nz"})   # row is no longer pending
client.post("/admin/correction/1/approve")
check("second approve writes nothing", STATE["ingredients"] == first, repr(STATE["ingredients"]))

reset()
set_edit({"ingredients": "rejected value\na\nb\nc"})
client.post("/admin/correction/1/reject")
check("reject changes nothing at all", recipe_snapshot() == {k: BASE[k] for k in BASE})
check("edit retained as rejected", EDIT_STATE["status"] == "rejected", EDIT_STATE["status"])

print("\n" + "=" * 64)
print(f"{len(fails)} FAILURE(S): {fails}" if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
