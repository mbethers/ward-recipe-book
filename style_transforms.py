"""House-style transforms, v1.2. Pure functions so they can be unit-tested."""
import re

VULGAR = {
    "¼": "1/4", "½": "1/2", "¾": "3/4", "⅓": "1/3", "⅔": "2/3",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}


def fix_fractions(t):
    """¼ -> 1/4, and 1½ -> 1 1/2 (a digit right before the glyph needs a space)."""
    if not t:
        return t
    out = []
    for ch in t:
        if ch in VULGAR:
            if out and out[-1].isdigit():
                out.append(" ")
            out.append(VULGAR[ch])
        else:
            out.append(ch)
    return "".join(out)


def fix_double_period(t):
    """Tbsp.. -> Tbsp.  Only after a unit, so real ellipses survive."""
    return re.sub(r"\b(Tbsp|tsp|oz|lb|hr|min|in)\.\.", r"\1.", t or "")


def fix_inches(t):
    """Adjectival inches get hyphenated with no period, and a leading '1' dropped.

    '1 9 in. crust' -> '9-in crust';  '9 inch dish' -> '9-in dish'.
    Only when a word follows, so 'roll out to 9 inches.' is left alone.
    """
    t = t or ""
    # drop a redundant leading "1" in front of the size: "1 9 in. crust".
    # Note the alternation rather than (?:in\.?)\b - a \b after an optional
    # period never matches, since "." and " " are both non-word characters.
    t = re.sub(r"(?<![\d/\-])1[ \t]+(\d+)[ \t]*(?:in\.|in\b|inch(?:es)?\b)(?=[ \t]+[A-Za-z])",
               r"\1-in", t)
    # "9 in." / "9in" / "9 inch" / "9-inch" -> "9-in", when a word follows
    t = re.sub(r"(\d+)[ \t]*-?[ \t]*(?:in\.|in\b|inch(?:es)?\b)(?=[ \t]+[A-Za-z])",
               r"\1-in", t)
    return t


def fix_oz(t):
    """oz/lb spacing + period, and drop a leading '1' before a package size."""
    t = t or ""
    t = re.sub(r"(?<![\d/])1[ \t]+(\d+)[ \t]*(oz|lb)s?\b\.?", r"\1 \2.", t)
    t = re.sub(r"(\d)[ \t]*(oz|lb)s?\b(?!\.)", r"\1 \2.", t)
    return t


def fix_card_units(t):
    """Recipe-card shorthand -> house style. C. -> cup(s), T. -> Tbsp., t. -> tsp.

    Anchored to a preceding quantity so a stray capital T mid-sentence is safe.
    """
    t = t or ""

    def cup(m):
        qty = m.group(1)
        whole = re.match(r"^(\d+)(?!\s*/)", qty.strip())
        plural = bool(whole and int(whole.group(1)) > 1)
        return f"{qty}cup{'s' if plural else ''}"

    t = re.sub(r"((?:\d+\s+)?\d+(?:/\d+)?\s+)C\.(?=\s)", cup, t)
    t = re.sub(r"((?:\d+\s+)?\d+(?:/\d+)?\s+)T\.(?=\s)", r"\1Tbsp.", t)
    t = re.sub(r"((?:\d+\s+)?\d+(?:/\d+)?\s+)t\.(?=\s)", r"\1tsp.", t)
    return t


def fix_tb(t):
    """Bare 'TB' -> 'Tbsp.' Seen as the D1 cookbook's only tablespoon spelling,
    consistently uppercase with no period. Absorbs an optional trailing period
    so this is idempotent regardless of execution order relative to the other
    BODY_ONLY functions."""
    return re.sub(r"\bTB\b\.?", "Tbsp.", t or "")


def fix_bare_tsp(t):
    """Bare 'tsp' -> 'tsp.' Seen as the D1 cookbook's only teaspoon spelling,
    consistently missing the house-style period. Same idempotency note as
    fix_tb."""
    return re.sub(r"\btsp\b\.?", "tsp.", t or "")


def fix_degrees(t):
    """A bare degree sign or the word 'degrees' -> °F. Never converts a value."""
    t = t or ""
    t = re.sub(r"(\d+)\s*°(?!\s*[FC])", r"\1°F", t)
    # Don't swallow a trailing sentence period - "425 degrees F." keeps its full stop.
    t = re.sub(r"(\d+)\s*degrees?\s*F\b", r"\1°F", t, flags=re.I)
    t = re.sub(r"(\d+)\s*degrees?\b", r"\1°F", t, flags=re.I)
    return t


# name/story get fractions only - they're prose and may contain real ellipses
BODY_ONLY = (fix_double_period, fix_card_units, fix_tb, fix_bare_tsp, fix_inches, fix_oz, fix_degrees)


def transform(field, value):
    out = fix_fractions(value or "")
    if field in ("ingredients", "instructions"):
        for fn in BODY_ONLY:
            out = fn(out)
    return out
