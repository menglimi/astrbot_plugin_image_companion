# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from typing import Any


PHOTO_GENERATION_SCOPES: tuple[str, ...] = (
    "private_owner",
    "private_friend",
    "group",
    "proactive",
)

PHOTO_GENERATION_SCOPE_LIMIT_KEYS: dict[str, str] = {
    "private_owner": "photo_generation_private_owner_max_daily",
    "private_friend": "photo_generation_private_friend_max_daily",
    "group": "photo_generation_group_max_daily",
    "proactive": "photo_generation_proactive_max_daily",
}

PHOTO_GENERATION_SCOPE_LABELS: dict[str, str] = {
    "private_owner": "主要用户私聊",
    "private_friend": "其他陪伴用户私聊",
    "group": "群聊",
    "proactive": "Bot 主动生图",
}


def normalize_photo_generation_scope_limit(value: Any, default: int = -1) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(-1, min(100, parsed))


def legacy_photo_generation_scope_limits(value: Any) -> dict[str, int]:
    """Convert the legacy allow-list into independent daily limits."""
    if value is None:
        return {scope: -1 for scope in PHOTO_GENERATION_SCOPES}

    raw = value
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                raw = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = re.split(r"[\n,，、;；]+", text)
        else:
            raw = []

    if isinstance(raw, dict):
        return {
            scope: normalize_photo_generation_scope_limit(
                raw.get(scope, raw.get(PHOTO_GENERATION_SCOPE_LIMIT_KEYS[scope], -1))
            )
            for scope in PHOTO_GENERATION_SCOPES
        }

    items = raw if isinstance(raw, (list, tuple, set)) else []
    selected = {
        str(item or "").strip().lower()
        for item in items
        if str(item or "").strip().lower() in PHOTO_GENERATION_SCOPES
    }
    return {scope: -1 if scope in selected else 0 for scope in PHOTO_GENERATION_SCOPES}
