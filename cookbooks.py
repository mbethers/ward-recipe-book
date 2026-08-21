"""Cookbook configuration and per-request resolution.

This app serves three cookbooks - University Ward (UW), Durham 1st Ward
(D1), and the Bethers Family - from one shared database and one Flask
process, picking which cookbook's data/branding to show based on the
incoming request's Host header. Each cookbook's custom domain is attached
to the same Render service; this module is the only place those real
hostnames need to be recorded.

Each cookbook supplies a `palette` - a full set of CSS custom-property
overrides (not just an accent color), emitted verbatim as a per-request
inline <style> block in base.html. UW/Family share the same warm neutral
palette (cream paper background, warm dark "ink") and differ only in their
accent; D1 overrides the neutrals too, for a genuinely different white/
black/green look built around its own icon art (see branding/d1/) rather
than just recoloring the same UW skin.

Every accent pair here was chosen by checking WCAG contrast against both
white and black text (see the git history for the actual numbers) - a
light/bright accent like D1's green reads fine as a filled button
background with black text on it, but fails badly as text-on-white, so
`terracotta`/`terracotta-dark` intentionally serve two different roles
(fill vs. standalone text/links) rather than being interchangeable, and
`on-accent` picks which text color pairs with a filled accent background.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Cookbook:
    slug: str
    name: str
    hostnames: frozenset
    palette: dict            # CSS custom-property name -> value, e.g. {"terracotta": "#c1613f"}
    icon_dir: str             # "" for UW's existing flat static/*.png, "d1/"/"family/" for the others
    admin_password_env: str
    allow_submissions: bool   # new recipes, corrections, added dish photos
    allow_reviews: bool       # star ratings/comments on existing recipes
    footer_tagline: str
    header_subtitle: str = ""  # shown under the name in the header, e.g. D1's compile credit
    header_title_lines: tuple = None  # forces a specific line break in the header title, e.g. D1's "...Ward" / "Family Cookbook"

    @property
    def title_lines(self):
        """The header title, split into the lines it should actually break
        on. Defaults to one line ("{name} Cookbook") unless a cookbook
        overrides header_title_lines to force a break at a specific word
        rather than wherever the browser happens to wrap it."""
        return self.header_title_lines or (f"{self.name} Cookbook",)


# UW and Family share these neutrals (warm paper/ink) and differ only in
# their accent pair - this is the palette both were already using live.
_WARM_NEUTRALS = {
    "cream": "#fbf6ee",
    "ink": "#2f2620",
    "muted": "#7a6f61",
    "sage": "#7f9473",
    "border": "#e8ddcb",
    "on-accent": "#fff",
}

COOKBOOKS = [
    Cookbook(
        slug="uw",
        name="University Ward",
        hostnames=frozenset({"uw-cookbook.bethers.dev"}),
        palette={**_WARM_NEUTRALS, "terracotta": "#c1613f", "terracotta-dark": "#a34f31"},
        icon_dir="",
        admin_password_env="ADMIN_PASSWORD_UW",
        allow_submissions=True,
        allow_reviews=True,
        footer_tagline="Mark Bethers and his buddy Claude Code are to blame for this app",
    ),
    Cookbook(
        slug="d1",
        name="Durham 1st Ward",
        hostnames=frozenset({"d1-cookbook.bethers.dev"}),
        # White background, black text, the bowl-and-spoon icon's own green
        # as the only color - not a recolor of UW's warm paper skin.
        # --terracotta (bright #7ed957, sampled from branding/d1/) fills
        # buttons/badges (paired with on-accent = black, ~12:1 contrast).
        # --terracotta-dark is a deliberately different, darker green
        # (~4.3:1 against both white and black) for standalone text/links
        # and hover fills - the bright shade fails badly as text-on-white
        # (~1.8:1). --sage reuses the same green rather than introducing a
        # second, competing green for category badges.
        palette={
            "cream": "#ffffff",
            "ink": "#000000",
            "muted": "#666666",
            "sage": "#7ed957",
            "border": "#e0e0e0",
            "on-accent": "#000000",
            "terracotta": "#7ed957",
            "terracotta-dark": "#3f8a20",
        },
        icon_dir="d1/",
        admin_password_env="ADMIN_PASSWORD_D1",
        # Locked historical snapshot: no new content, no interaction at all -
        # see the plan this shipped under for the full reasoning.
        allow_submissions=False,
        allow_reviews=False,
        footer_tagline="Mark Bethers and his buddy Claude Code are to blame for this app",
        header_subtitle="Compiled May 2024 by Kendra Johnson",
        header_title_lines=("Durham 1st Ward", "Family Cookbook"),
    ),
    Cookbook(
        slug="family",
        name="Bethers Family",
        hostnames=frozenset({"family-cookbook.bethers.dev"}),
        palette={**_WARM_NEUTRALS, "terracotta": "#c47026", "terracotta-dark": "#8a4d18"},
        icon_dir="family/",
        admin_password_env="ADMIN_PASSWORD_FAMILY",
        allow_submissions=True,
        allow_reviews=True,
        footer_tagline="Mark Bethers and his buddy Claude Code are to blame for this app",
    ),
]

BY_SLUG = {c.slug: c for c in COOKBOOKS}

_BY_HOSTNAME = {}
for _c in COOKBOOKS:
    for _h in _c.hostnames:
        _BY_HOSTNAME[_h.lower()] = _c


def resolve_cookbook(host: str):
    """Maps a request's Host header to a Cookbook, or None if unrecognized.

    Deliberately no fallback to any single cookbook, ever - an unrecognized
    Host (a typo, or Render's own onrender.com default hostname for this
    service, left unmapped on purpose) must 404, not quietly show one
    cookbook's real data under an unbranded URL.

    FORCE_COOKBOOK (set only for local/Docker testing, must stay unset in
    production) short-circuits this entirely, so tests can pick a cookbook
    without needing real DNS for all three domains.
    """
    forced = os.environ.get("FORCE_COOKBOOK", "").strip().lower()
    if forced:
        return BY_SLUG.get(forced)

    host = (host or "").split(":")[0].strip().lower()
    return _BY_HOSTNAME.get(host)
