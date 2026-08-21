"""Cookbook configuration and per-request resolution.

This app serves three cookbooks - University Ward (UW), Durham 1st Ward
(D1), and the Bethers Family - from one shared database and one Flask
process, picking which cookbook's data/branding to show based on the
incoming request's Host header. Each cookbook's custom domain is attached
to the same Render service; this module is the only place those real
hostnames need to be recorded.

Accent colors are drawn from each cookbook's own icon art (see branding/),
not an arbitrary palette - they replace the two CSS variables (`--terracotta`
/ `--terracotta-dark`) already used pervasively throughout style.css, via a
small inline <style> override in base.html. Every pair here was checked for
WCAG contrast against white text (used by buttons/badges) before picking it.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Cookbook:
    slug: str
    name: str
    hostnames: frozenset
    accent_primary: str      # --terracotta override - links, buttons, borders
    accent_secondary: str    # --terracotta-dark override - hover/active/dark-badge states
    icon_dir: str             # "" for UW's existing flat static/*.png, "d1/"/"family/" for the others
    admin_password_env: str
    allow_submissions: bool   # new recipes, corrections, added dish photos
    allow_reviews: bool       # star ratings/comments on existing recipes
    footer_tagline: str


COOKBOOKS = [
    Cookbook(
        slug="uw",
        name="University Ward",
        hostnames=frozenset({"uw-cookbook.bethers.dev"}),
        accent_primary="#c1613f",
        accent_secondary="#a34f31",
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
        accent_primary="#5a3619",
        accent_secondary="#3e220d",
        icon_dir="d1/",
        admin_password_env="ADMIN_PASSWORD_D1",
        # Locked historical snapshot: no new content, no interaction at all -
        # see the plan this shipped under for the full reasoning.
        allow_submissions=False,
        allow_reviews=False,
        footer_tagline="Mark Bethers and his buddy Claude Code are to blame for this app",
    ),
    Cookbook(
        slug="family",
        name="Bethers Family",
        hostnames=frozenset({"family-cookbook.bethers.dev"}),
        accent_primary="#c47026",
        accent_secondary="#8a4d18",
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
