"""End-to-end admin flow against a stateful stub DB. Scratch harness."""
import os
import re
import sys

os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "testpass"
os.environ["FLASK_SECRET_KEY"] = "test-key"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as flaskapp  # noqa: E402
import db  # noqa: E402

ISSUES = [
    'Measurement abbreviations: "T." should be "Tbsp."',
    'Fractions: ¼ should be written as 1/4',
    '"1 8oz. cream cheese" → "8 oz. cream cheese"',
]
STATE = {
    "id": 7, "name": "Blueberry Crumble Pie", "submitter_name": "Kaylyn Betancourt",
    "category": "Dessert", "cuisine": "American", "dietary_tags": "",
    "prep_time": "1 hr", "servings": "8",
    "ingredients": "1 T. butter\n¼ cup sugar\n1 8oz. cream cheese",
    "instructions": "1. Preheat oven to 350°F.", "story": "", "photo_path": None,
    "source_image_path": "https://example.test/s.jpg", "source_url": "",
    "status": "published", "submitted_at": "2026-08-17T20:00:00+00:00",
    "reviewed_at": None, "parse_model": "claude-sonnet-5",
    "proofreading_notes": "\n".join(ISSUES),
}


class R:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Conn:
    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("update recipes"):
            for col, val in zip(re.findall(r"(\w+)\s*=\s*%s", sql), params or []):
                STATE[col] = val
            return R([])
        if s.startswith(("insert", "delete", "update")): return R([{"id": 7}])
        if "from recipes" in s: return R([dict(STATE)])
        if "from settings" in s: return R([{"value": "intro"}])
        return R([])
    def commit(self): pass


db.get_db = lambda: Conn()
flaskapp.db.get_db = lambda: Conn()
client = flaskapp.app.test_client()
with client.session_transaction() as s:
    s["is_admin"] = True

fails = []


def listed():
    body = client.get("/admin/recipe/7").get_data(as_text=True)
    return re.findall(r'class="issue-text">([^<]*)<', body)


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if not cond else ""))
    if not cond:
        fails.append(label)


print("Start: 3 issues listed\n")
check("3 rows shown", len(listed()) == 3, str(listed()))

# Walk the real UI path: click Fix on row 1 -> preview -> Accept.
print("\nFix -> preview -> Accept, three times:\n")
for round_no in range(3):
    before = listed()
    body = client.get("/admin/recipe/7").get_data(as_text=True)
    # scrape the first Fix form exactly as the browser would submit it
    form = re.search(r'<form[^>]*preview-fix[^>]*>(.*?)</form>', body, re.S).group(1)
    issue = re.search(r'name="issue" value="([^"]*)"', form).group(1)
    idx = re.search(r'name="issue_index" value="([^"]*)"', form).group(1)
    import html as _h
    issue = _h.unescape(issue)

    flaskapp.propose_recipe_fix = lambda *a, **k: {
        "success": True, "fixed_field": "ingredients", "original": "orig",
        "options": [{"fixed": f"fixed-round-{round_no}", "explanation": "e"},
                    {"fixed": "alt", "explanation": "e2"}],
    }
    prev = client.post("/admin/recipe/7/preview-fix", data={"issue": issue, "issue_index": idx})
    check(f"round {round_no+1}: preview 200", prev.status_code == 200, str(prev.status_code))
    pbody = prev.get_data(as_text=True)
    carried = re.search(r'name="issue_index" value="([^"]*)"', pbody)
    check(f"round {round_no+1}: index carried to preview", carried is not None and carried.group(1) == idx)

    client.post("/admin/recipe/7/accept-fix", data={
        "issue": issue, "issue_index": idx, "chosen_fix": f"fixed-round-{round_no}",
        "fixed_field": "ingredients", "original": "orig"})
    after = listed()
    check(f"round {round_no+1}: one fewer row ({len(before)} -> {len(after)})", len(after) == len(before) - 1)
    check(f"round {round_no+1}: resolved issue gone", issue not in after, issue)

check("all issues cleared at the end", listed() == [], str(listed()))
check("field updated by last accept", STATE["ingredients"] == "fixed-round-2", STATE["ingredients"])
check("notes column empty", STATE["proofreading_notes"] == "", repr(STATE["proofreading_notes"]))

print("\nReject leaves everything alone:\n")
STATE["proofreading_notes"] = "\n".join(ISSUES)
STATE["ingredients"] = "untouched"
client.post("/admin/recipe/7/reject-fix")
check("all 3 issues still listed", len(listed()) == 3, str(len(listed())))
check("ingredients untouched", STATE["ingredients"] == "untouched", STATE["ingredients"])

print("\n" + "=" * 62)
print(f"{len(fails)} FAILURE(S): {fails}" if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
