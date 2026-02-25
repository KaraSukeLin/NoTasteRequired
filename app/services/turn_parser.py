from __future__ import annotations

import json
import re
from typing import Any

from app.models import OutfitCandidate


def parse_profile_updates(message: str) -> dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {}

    keys = ("scenario", "primary_scene", "brand", "preferences", "exclusions")
    updates: dict[str, Any] = {}

    scenario = _extract_keyed_value(text, "scenario", keys)
    if scenario:
        updates["scenario"] = scenario

    primary_scene = _extract_keyed_value(text, "primary_scene", keys)
    if primary_scene:
        updates["primary_scene"] = primary_scene

    brand = _extract_keyed_value(text, "brand", keys)
    if brand:
        updates["brand"] = _normalize_brand(brand)

    preferences = _extract_keyed_list(text, "preferences", keys)
    if preferences:
        updates["preferences"] = preferences

    exclusions = _extract_keyed_list(text, "exclusions", keys)
    if exclusions:
        updates["exclusions"] = exclusions

    return updates


def extract_feedback_hint(
    message: str,
    outfits: list[OutfitCandidate],
) -> tuple[str | None, list[str]]:
    text = (message or "").strip()
    if not text:
        return None, []

    keys = ("preserve_outfit_id", "preserve_outfit_title", "replace_categories")
    preserve_outfit_id = _extract_keyed_value(text, "preserve_outfit_id", keys)

    if not preserve_outfit_id:
        title = _extract_keyed_value(text, "preserve_outfit_title", keys)
        if title:
            match = _match_outfit_by_title(title, outfits)
            if match is not None:
                preserve_outfit_id = match.outfit_id

    replace_categories = _extract_keyed_list(text, "replace_categories", keys)
    replace_categories = [
        normalize_category(category)
        for category in replace_categories
        if normalize_category(category)
    ]
    replace_categories = list(dict.fromkeys(replace_categories))

    if not preserve_outfit_id and not replace_categories:
        return None, []

    return preserve_outfit_id, replace_categories


def normalize_category(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""

    if value in {"外套", "jacket", "coat", "outerwear"}:
        return "外套"
    if value in {"上身", "上衣", "shirt", "top", "tee", "t-shirt", "tshirt"}:
        return "上身"
    if value in {"下身", "褲", "bottom", "pants", "trousers"}:
        return "下身"
    if value in {"鞋子", "鞋", "shoes", "shoe", "sneakers"}:
        return "鞋子"
    if value in {"配件", "accessory", "accessories"}:
        return "配件"

    return raw.strip()


def _extract_keyed_value(
    text: str,
    key: str,
    known_keys: tuple[str, ...],
) -> str | None:
    keys_pattern = "|".join(re.escape(item) for item in known_keys)
    pattern = re.compile(
        rf"(?is)(?:^|[\n\r;；])\s*{re.escape(key)}\s*[:：]\s*(.+?)\s*(?=(?:[\n\r;；]\s*(?:{keys_pattern})\s*[:：])|$)"
    )
    match = pattern.search(text)
    if not match:
        return None

    value = match.group(1).strip().strip(",;；")
    return value or None


def _extract_keyed_list(
    text: str,
    key: str,
    known_keys: tuple[str, ...],
) -> list[str]:
    raw = _extract_keyed_value(text, key, known_keys)
    if not raw:
        return []

    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                values = [str(item).strip() for item in loaded if str(item).strip()]
                return list(dict.fromkeys(values))
        except Exception:
            pass

    values = [
        part.strip(" ,;；")
        for part in re.split(r"[,，、|\n\r;；]+", raw)
        if part.strip(" ,;；")
    ]
    return list(dict.fromkeys(values))


def _normalize_brand(raw: str) -> str:
    token = raw.strip()
    if not token:
        return token
    if re.fullmatch(r"[A-Za-z0-9\-\s]+", token):
        return token.upper()
    return token


def _match_outfit_by_title(title: str, outfits: list[OutfitCandidate]) -> OutfitCandidate | None:
    if not outfits:
        return None

    target = title.strip()
    if not target:
        return None

    by_length = sorted(outfits, key=lambda outfit: len(outfit.title or ""), reverse=True)
    for outfit in by_length:
        if outfit.title and outfit.title == target:
            return outfit

    return None
