"""Pulls a recipe out of a webpage the submitter linked to.

Two strategies, cheapest/most-reliable first:
1. Most recipe sites embed structured "Recipe" data (schema.org JSON-LD) for
   Google/Pinterest's benefit - if it's there, use it directly. Free, fast,
   no AI involved, and more accurate than anything an LLM would guess.
2. Otherwise, fall back to sending the page's visible text to Claude, same
   idea as ai_parse.py's photo/PDF transcription.
"""
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import anthropic
from ai_parse import ParseError, _client, _extract_json
from db import CATEGORIES, CUISINES

MAX_REDIRECTS = 5
FETCH_TIMEOUT = 12.0
MAX_PAGE_BYTES = 3 * 1024 * 1024  # 3 MB - plenty for an HTML recipe page
MAX_TEXT_CHARS = 15000  # bound what we send to Claude for the text fallback

WEB_SYSTEM_PROMPT = f"""You are extracting a recipe from the visible text of a \
webpage, for a church ward recipe book. The text may include unrelated clutter \
(navigation, ads, comments, unrelated articles) - ignore anything that isn't \
part of the recipe itself.

Return ONLY valid JSON, no preamble, no markdown code fences, no commentary. Use \
exactly this shape:

{{
  "name": "recipe name",
  "category": "one of {", ".join(CATEGORIES)} - your best guess",
  "cuisine": "one of {", ".join(CUISINES)} - your best guess, or empty string if you can't tell",
  "prep_time": "total time to make it, as stated on the page (e.g. '30 min', '1 hr 15 min'), or empty string if not stated",
  "servings": "how many it serves/yields, as stated on the page (e.g. '8', '8-10', 'makes 2 dozen'), or empty string if not stated",
  "ingredients": ["ingredient line 1", "ingredient line 2"],
  "instructions": ["step 1", "step 2"],
  "story": "any personal note, memory, or story the author included, or empty string if none"
}}

Rules:
- Keep each ingredient as its own array entry (e.g. "2 cups flour"), not one big blob.
- Keep each instruction step as its own array entry.
- If this page doesn't appear to contain a recipe at all, use empty strings/arrays \
for every field rather than inventing one.
- Do not add ingredients, steps, or commentary that are not on the page.
- Do not guess at prep_time or servings if the page doesn't actually state them - \
leave them blank rather than estimating.
- Do NOT guess at dietary/allergen suitability (vegan, gluten-free, dairy-free, etc.) - \
that isn't part of this task and is left for a human to confirm.
"""


class FetchError(RuntimeError):
    """URL couldn't be fetched at all (blocked, unreachable, not HTML, etc.)."""


def _validate_public_url(url: str) -> str:
    """Raises FetchError unless url is a plain http(s) URL pointing at a public
    address - blocks localhost/private-network/link-local targets so this
    can't be used to probe the server's own network (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("That doesn't look like a web link (needs to start with http:// or https://).")
    if not parsed.hostname:
        raise FetchError("That doesn't look like a valid web link.")

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise FetchError(f"Couldn't find that website ({parsed.hostname}).")

    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError("That link points somewhere this app isn't allowed to fetch from.")
    return url


def _fetch_html(url: str) -> tuple[str, str]:
    """Fetches url, following redirects manually so each hop gets the same
    SSRF validation as the original URL. Returns (final_url, html_text)."""
    current = _validate_public_url(url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WardCookbookBot/1.0)"}

    for _ in range(MAX_REDIRECTS):
        try:
            resp = requests.get(
                current, headers=headers, timeout=FETCH_TIMEOUT,
                allow_redirects=False, stream=True,
            )
        except requests.RequestException as exc:
            raise FetchError(f"Couldn't reach that page: {exc}") from exc

        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise FetchError("That link redirected somewhere we couldn't follow.")
            current = _validate_public_url(requests.compat.urljoin(current, location))
            continue

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            resp.close()
            raise FetchError("That link doesn't point to a regular webpage.")

        raw = resp.raw.read(MAX_PAGE_BYTES + 1, decode_content=True)
        resp.close()
        if len(raw) > MAX_PAGE_BYTES:
            raise FetchError("That page is too large to read.")
        return current, raw.decode(resp.encoding or "utf-8", errors="replace")

    raise FetchError("That link redirected too many times.")


_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?$")


def _friendly_duration(iso: object) -> str:
    """Converts a schema.org duration like 'PT1H15M' to '1 hr 15 min'.
    Returns '' if it isn't a duration string we recognize."""
    if not isinstance(iso, str):
        return ""
    match = _ISO_DURATION.match(iso.strip())
    if not match:
        return ""
    days, hours, minutes = match.groups()
    if not (days or hours or minutes):
        return ""
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != '1' else ''}")
    if hours:
        parts.append(f"{hours} hr")
    if minutes:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def _friendly_yield(value: object) -> str:
    """schema.org recipeYield can be a string, a number, or a list of either."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _from_json_ld(soup: BeautifulSoup) -> dict | None:
    """Looks for schema.org Recipe structured data. Most modern recipe sites
    include this for Google's benefit - when present it's more reliable than
    an AI guess, and free."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        # Some sites wrap everything in a top-level @graph array.
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = data["@graph"]

        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "Recipe" not in types:
                continue

            ingredients = item.get("recipeIngredient") or item.get("ingredients") or []
            if isinstance(ingredients, str):
                ingredients = [ingredients]

            raw_instructions = item.get("recipeInstructions") or []
            instructions = []
            if isinstance(raw_instructions, str):
                instructions = [raw_instructions]
            elif isinstance(raw_instructions, list):
                for step in raw_instructions:
                    if isinstance(step, str):
                        instructions.append(step)
                    elif isinstance(step, dict):
                        # HowToStep, or a HowToSection with nested itemListElement
                        if step.get("@type") == "HowToSection" and isinstance(step.get("itemListElement"), list):
                            for sub in step["itemListElement"]:
                                if isinstance(sub, dict) and sub.get("text"):
                                    instructions.append(sub["text"])
                        elif step.get("text"):
                            instructions.append(step["text"])

            cuisine = item.get("recipeCuisine") or ""
            if isinstance(cuisine, list):
                cuisine = cuisine[0] if cuisine else ""
            cuisine = cuisine.strip() if isinstance(cuisine, str) else ""
            if cuisine not in CUISINES:
                cuisine = "Other" if cuisine else ""

            category = item.get("recipeCategory") or ""
            if isinstance(category, list):
                category = category[0] if category else ""
            category = category.strip() if isinstance(category, str) else ""
            if category not in CATEGORIES:
                category = "Other"

            name = item.get("name") or ""
            description = item.get("description") or ""
            if not isinstance(description, str):
                description = ""

            if not (name and ingredients and instructions):
                # Missing the essentials - not confident enough to skip the AI fallback.
                continue

            prep_time = (
                _friendly_duration(item.get("totalTime"))
                or _friendly_duration(item.get("prepTime"))
                or _friendly_duration(item.get("cookTime"))
            )
            servings = _friendly_yield(item.get("recipeYield"))

            return {
                "name": name.strip(),
                "category": category,
                "cuisine": cuisine,
                "prep_time": prep_time,
                "servings": servings,
                "ingredients": "\n".join(str(i).strip() for i in ingredients if str(i).strip()),
                "instructions": "\n".join(str(s).strip() for s in instructions if str(s).strip()),
                "story": description.strip()[:1000],
                "parse_model": "site structured data",
            }
    return None


def _from_page_text(html: str, use_better_model: bool) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)[:MAX_TEXT_CHARS]
    if not text.strip():
        raise ParseError("That page doesn't seem to have any readable text.")

    from ai_parse import DEFAULT_MODEL, BETTER_MODEL
    model = BETTER_MODEL if use_better_model else DEFAULT_MODEL
    client = _client()
    try:
        message = client.messages.create(
            model=model,
            max_tokens=2000,
            system=WEB_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Webpage text:\n\n{text}"}],
        )
    except anthropic.APITimeoutError as exc:
        raise ParseError("Claude took too long to respond (timed out).") from exc
    except anthropic.APIError as exc:
        raise ParseError(f"Claude API error: {exc}") from exc

    raw_text = "".join(block.text for block in message.content if block.type == "text")
    data = _extract_json(raw_text)

    category = (data.get("category") or "").strip()
    if category not in CATEGORIES:
        category = "Other"
    cuisine = (data.get("cuisine") or "").strip()
    if cuisine not in CUISINES:
        cuisine = "Other" if cuisine else ""

    ingredients = data.get("ingredients") or []
    instructions = data.get("instructions") or []
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    if isinstance(instructions, str):
        instructions = [instructions]

    return {
        "name": (data.get("name") or "").strip(),
        "category": category,
        "cuisine": cuisine,
        "prep_time": (data.get("prep_time") or "").strip(),
        "servings": (data.get("servings") or "").strip(),
        "ingredients": "\n".join(str(i).strip() for i in ingredients if str(i).strip()),
        "instructions": "\n".join(str(s).strip() for s in instructions if str(s).strip()),
        "story": (data.get("story") or "").strip(),
        "parse_model": model,
    }


def parse_recipe_from_url(url: str, use_better_model: bool = False) -> tuple[dict, str]:
    """Returns (parsed_recipe_dict, final_url_after_redirects). Raises
    FetchError if the URL couldn't be fetched, or ParseError if it was
    fetched but no recipe could be made of it."""
    final_url, html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    structured = _from_json_ld(soup)
    if structured:
        return structured, final_url

    return _from_page_text(html, use_better_model), final_url
