"""Sends a recipe photo or PDF to Claude and gets back structured JSON."""
import base64
import json
import os
import re

import anthropic

from db import CATEGORIES

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
BETTER_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are transcribing a handwritten or printed recipe card, photo, \
or PDF for a church ward recipe book. Read the image carefully and extract the recipe.

Return ONLY valid JSON, no preamble, no markdown code fences, no commentary. Use \
exactly this shape:

{
  "name": "recipe name",
  "category": "one of Main, Side, Dessert, Bread, Breakfast, Other - your best guess",
  "ingredients": ["ingredient line 1", "ingredient line 2"],
  "instructions": ["step 1", "step 2"],
  "story": "any personal note, memory, or story visible on the card, or empty string if none"
}

Rules:
- If any word or section is illegible, write [unclear] in its place rather than guessing.
- Keep each ingredient as its own array entry (e.g. "2 cups flour"), not one big blob.
- Keep each instruction step as its own array entry.
- If you cannot determine a field at all, use an empty string or empty array - never \
omit a key.
- Do not add ingredients, steps, or commentary that are not visibly on the source.
"""


class ParseError(RuntimeError):
    pass


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ParseError("ANTHROPIC_API_KEY isn't set.")
    return anthropic.Anthropic(api_key=api_key)


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
    except anthropic.APIError as exc:
        raise ParseError(f"Claude API error: {exc}") from exc

    raw_text = "".join(block.text for block in message.content if block.type == "text")
    data = _extract_json(raw_text)

    category = (data.get("category") or "").strip()
    if category not in CATEGORIES:
        category = "Other"

    ingredients = data.get("ingredients") or []
    instructions = data.get("instructions") or []
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    if isinstance(instructions, str):
        instructions = [instructions]

    return {
        "name": (data.get("name") or "").strip(),
        "category": category,
        "ingredients": "\n".join(str(i).strip() for i in ingredients if str(i).strip()),
        "instructions": "\n".join(str(s).strip() for s in instructions if str(s).strip()),
        "story": (data.get("story") or "").strip(),
        "parse_model": model,
    }
