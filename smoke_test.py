"""Drive the admin routes against a stub database.

Catches the class of bug that repeatedly reached production here: Jinja
constructs that only fail at render time, forms that submit to the wrong
place, and fix proposals that overwrite a whole field.

    ./venv/bin/python3 smoke_test.py
"""
import html
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


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Conn:
    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("update recipes"):
            for col, val in zip(re.findall(r"(\w+)\s*=\s*%s", sql), params or []):
                STATE[col] = val
            return Result([])
        if s.startswith(("insert", "delete", "update")):
            return Result([{"id": 7}])
        if "from recipes" in s:
            return Result([dict(STATE)])
        if "from settings" in s:
            return Result([{"value": "intro"}])
        return Result([])

    def commit(self): pass


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
for path in ("/admin/queue", "/admin/published"):
    check(f"{path} 200", client.get(path).status_code == 200)

print("\n" + "=" * 64)
print(f"{len(fails)} FAILURE(S): {fails}" if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
