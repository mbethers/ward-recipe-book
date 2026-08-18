"""Sends a recipe photo or PDF to Claude and gets back structured JSON."""
import base64
import json
import logging
import os
import re

import anthropic

from db import CATEGORIES, CUISINES

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
BETTER_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = f"""You are transcribing a handwritten or printed recipe card, photo, \
or PDF for a church ward recipe book. Read the image carefully and extract the recipe.

Return ONLY valid JSON, no preamble, no markdown code fences, no commentary. Use \
exactly this shape:

{{
  "name": "recipe name",
  "category": "one of {", ".join(CATEGORIES)} - your best guess",
  "cuisine": "one of {", ".join(CUISINES)} - your best guess, or empty string if you can't tell",
  "prep_time": "total time to make it, as written on the card (e.g. '30 min', '1 hr 15 min'), or empty string if not shown",
  "servings": "how many it serves/yields, as written on the card (e.g. '8', '8-10', 'makes 2 dozen'), or empty string if not shown",
  "ingredients": ["ingredient line 1", "ingredient line 2"],
  "instructions": ["step 1", "step 2"],
  "story": "any personal note, memory, or story visible on the card, or empty string if none"
}}

Rules:
- If any word or section is illegible, write [unclear] in its place rather than guessing.
- Keep each ingredient as its own array entry (e.g. "2 cups flour"), not one big blob.
- Keep each instruction step as its own array entry.
- If you cannot determine a field at all, use an empty string or empty array - never \
omit a key.
- Do not add ingredients, steps, or commentary that are not visibly on the source.
- Do not guess at prep_time or servings if they aren't actually written on the card -
leave them blank rather than estimating.
- Do NOT guess at dietary/allergen suitability (vegan, gluten-free, dairy-free, etc.) - \
that isn't part of this task and is left for a human to confirm.
"""


class ParseError(RuntimeError):
    pass


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ParseError("ANTHROPIC_API_KEY isn't set.")
    # Bounded well under the gunicorn worker timeout (render.yaml: --timeout 120)
    # so a slow/retried call fails cleanly with a ParseError instead of the
    # whole request getting killed by the server before Flask can respond.
    return anthropic.Anthropic(api_key=api_key, timeout=45.0, max_retries=1)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Claude's response wasn't valid JSON: {exc}") from exc


def parse_recipe(file_bytes: bytes, media_type: str, use_better_model: bool = False) -> dict:
    """media_type is 'application/pdf' or an image/* mime type (already normalized)."""
    model = BETTER_MODEL if use_better_model else DEFAULT_MODEL
    client = _client()

    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(file_bytes).decode()},
        }
    else:
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(file_bytes).decode()},
        }

    try:
        message = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    content_block,
                    {"type": "text", "text": "Extract this recipe as JSON per the system instructions."},
                ],
            }],
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


def parse_recipe_multi(files_with_types: list[tuple[bytes, str]], use_better_model: bool = False) -> dict:
    """Parse multiple recipe images/PDFs together. files_with_types is a list of
    (file_bytes, media_type) tuples. Sends all to Claude in a single call for unified
    context (e.g., 'these 3 pages are all from the same recipe')."""
    if not files_with_types:
        raise ParseError("No files provided to parse_recipe_multi")

    model = BETTER_MODEL if use_better_model else DEFAULT_MODEL
    client = _client()

    # Build content blocks for all files
    content_blocks = []
    for file_bytes, media_type in files_with_types:
        if media_type == "application/pdf":
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(file_bytes).decode()},
            })
        else:
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(file_bytes).decode()},
            })

    # Add the instruction
    content_blocks.append({
        "type": "text",
        "text": "Extract this recipe as JSON per the system instructions. These are all pages/photos from the same recipe.",
    })

    try:
        message = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": content_blocks,
            }],
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


PROOFREAD_SYSTEM_PROMPT = """You are reviewing a submission to a church ward recipe book \
before it's published, looking for three kinds of problems:

1. Clear, high-confidence spelling or wording mistakes - e.g. "Belgium Waffles" almost \
certainly should be "Belgian Waffles" (country name used instead of the correct adjective).

2. Signs the ingredients/instructions are unedited AI-chat or copy-paste output rather \
than a clean recipe - e.g. emoji used as section headers (🍋, 🛒, ⭐, 🔥), the recipe \
title or an "Ingredients"/"Instructions" label repeated inside the ingredients/\
instructions text itself, leftover markdown dividers like "---", conversational asides \
addressed to the reader ("don't worry, it thickens naturally on its own"), or entire \
bonus sections that aren't actually ingredients or steps (e.g. "Why this recipe works", \
"Pro tips"). Normal recipe organization like "For the crust:" or "Part 1: crust" is fine \
and should NOT be flagged - only flag actual leftover chat/markdown artifacts and \
non-recipe content. Flag this as one combined issue naming what looks unedited, not one \
issue per line.

3. Formatting inconsistencies against the cookbook's house style guide - flag:
   - Measurement abbreviations missing their period or not standardized. EVERY \
     abbreviated unit takes a period: "Tbsp." not "tbsp"/"T"/"Tablespoon"; "tsp." not \
     "ts"/"t"/"teaspoon"; "oz." not "oz"; "lb." not "lb"/"lbs". "cup" is spelled out, \
     never abbreviated, and takes no period.
   - Fractions written as special superscript/vulgar characters (¼, ½, ¾, ⅓, ⅔) - these \
     should be plain slash form instead: "1/4", "1/2", "3/4", "1/3", "2/3". Flag so they \
     can be converted back to slash form.
   - A redundant leading "1" in front of a container/package size: "1 8oz. cream cheese" \
     should be "8 oz. cream cheese" - drop the leading "1" and put a space between the \
     number and the unit. Only when the leading count is exactly 1. If the leading count \
     is 2 or more ("3 8oz. cans"), leave that line alone entirely - do NOT flag it.
   - Ingredient lines not matching pattern "number unit. [descriptor] ingredient [prep]" \
     e.g. "melted butter" should be "butter, melted" (prep after); "avocado oil" is fine \
     (descriptor is part of name)
   - Capitalized ingredient names that shouldn't be: "Cornstarch" should be "cornstarch", \
     "Butter" should be "butter" (unless it's a brand name like "Skippy peanut butter")

Return ONLY valid JSON, no preamble, no markdown code fences: {"issues": ["short note", ...]}

Do NOT flag:
- Personal names, nicknames, or family names (e.g. "Grammy Jo's Fruit Salad" is fine as-is)
- Regional, ethnic, or foreign ingredient/dish names that are correct as written
- Informal or conversational phrasing, abbreviations, or normal style choices
- A short personal story/memory in the story field - that's wanted content, not clutter
- Anything you are not genuinely confident is actually a problem

Flag at most 5 issues. If nothing meets that bar, return {"issues": []} - most \
submissions should get an empty list. Each issue should be one short, specific \
sentence (e.g. "\\"Belgium Waffles\\" - did you mean \\"Belgian Waffles\\"?").
"""


def proofread_recipe(name: str, ingredients: str, instructions: str, story: str) -> list[str]:
    """Best-effort spelling/wording check. Never raises - on any failure
    (no API key, timeout, bad response) it just returns no issues, since this
    is a nice-to-have that should never block a submission."""
    try:
        client = _client()
        text = (
            f"Recipe name: {name}\n\nIngredients:\n{ingredients}\n\n"
            f"Instructions:\n{instructions}\n\nStory/memory: {story}"
        )
        message = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=500,
            system=PROOFREAD_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw_text = "".join(block.text for block in message.content if block.type == "text")
        data = _extract_json(raw_text)
        issues = data.get("issues") or []

        # Ensure issues is always a list (sometimes Claude returns it as a string)
        if isinstance(issues, str):
            # If it's a string, split by newline or common delimiters
            issues = [i.strip() for i in issues.split('\n') if i.strip()]

        return [str(i).strip() for i in issues if str(i).strip()][:5]
    except Exception as exc:
        logging.getLogger(__name__).warning("Proofreading skipped: %s", exc)
        return []


def propose_recipe_fix(name: str, ingredients: str, instructions: str, story: str, issue: str) -> dict:
    """Propose a fix for a specific proofreading issue without applying it.
    Returns options for the admin to review and choose from.
    Returns a dict with 'success', 'fixed_field', 'options' keys.
    Each option has 'original', 'fixed', 'explanation'.
    """
    try:
        client = _client()
        prompt = f"""You are proposing a fix for a specific issue in a recipe that was flagged during review.

Current recipe:
- Name: {name}
- Ingredients: {ingredients}
- Instructions: {instructions}
- Story: {story}

Issue to fix: {issue}

Propose ONE or TWO ways to fix this issue (not more). Do NOT apply the fix - just suggest it.
Return ONLY valid JSON with this shape:

{{
  "fixed_field": "name" | "ingredients" | "instructions" | "story",
  "original": "the exact original text that needs fixing",
  "options": [
    {{
      "fixed": "proposed fix option 1",
      "explanation": "why this is the right fix"
    }},
    {{
      "fixed": "proposed fix option 2 (if applicable)",
      "explanation": "alternative approach"
    }}
  ]
}}

If there's only one sensible way to fix it, include only one option - do not invent a
second option just to have two.

If the issue is about measurement abbreviations or fractions, fix ALL occurrences in the
relevant field (ingredients or instructions). House style for those:
- Every abbreviated unit takes a period: "Tbsp.", "tsp.", "oz.", "lb.". "cup" is spelled
  out with no period.
- Fractions are plain slash form: "1/4", "1/2", "3/4". NEVER use the superscript/vulgar
  characters (¼, ½, ¾) - convert any of those to slash form.
- A leading "1" before a package size is dropped and the unit is spaced:
  "1 8oz. cream cheese" becomes "8 oz. cream cheese". This applies ONLY when the leading
  count is exactly 1. If the count is 2 or more ("3 8oz. cans"), leave that line exactly
  as written.

IMPORTANT: All temperatures are in Fahrenheit. Never convert or change temperature values.
Show the complete fixed text for the admin to review.
"""
        message = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = "".join(block.text for block in message.content if block.type == "text")
        data = _extract_json(raw_text)

        return {
            "success": True,
            "fixed_field": data.get("fixed_field"),
            "original": data.get("original"),
            "options": data.get("options", []),
        }
    except Exception as exc:
        logging.getLogger(__name__).warning("Propose fix failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
        }


def apply_recipe_fix_choice(name: str, ingredients: str, instructions: str, story: str, issue: str, chosen_fix: str) -> dict:
    """Apply the admin's chosen fix from the proposed options.
    The chosen_fix is the exact text they selected from the options.
    Returns success/error."""
    try:
        # Validate which field is being fixed by looking for the original text
        all_text = f"{name}|{ingredients}|{instructions}|{story}"

        # Determine which field contains the change
        if chosen_fix in name or (name and chosen_fix.replace("'", '"') in name):
            fixed_field = "name"
            updated_value = chosen_fix
        elif ingredients and (chosen_fix in ingredients or len(ingredients) > 100):
            # Multi-line field - do replacement
            fixed_field = "ingredients"
            updated_value = chosen_fix
        elif instructions and (chosen_fix in instructions or len(instructions) > 100):
            fixed_field = "instructions"
            updated_value = chosen_fix
        else:
            fixed_field = "story"
            updated_value = chosen_fix

        return {
            "success": True,
            "fixed_field": fixed_field,
            "fixed": updated_value,
        }
    except Exception as exc:
        logging.getLogger(__name__).warning("Apply choice failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
        }
