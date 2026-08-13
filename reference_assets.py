# -*- coding: utf-8 -*-
"""Shared normalization helpers for member, relationship-role, and knowledge references."""
from __future__ import annotations

import re
import time
import uuid
from typing import Any


REFERENCE_ASSET_VERSION = 1
REFERENCE_ASSET_MAX_BYTES = 12 * 1024 * 1024
REFERENCE_ASSET_MAX_TOTAL = 160
REFERENCE_ASSET_MAX_PER_OWNER = 8
REFERENCE_ASSET_MAX_TAGS = 12
REFERENCE_ASSET_MAX_TEXT = 500
REFERENCE_ASSET_SCOPES = {"relation_user", "relation_role", "knowledge"}
REFERENCE_ASSET_ROLES = ("identity", "outfit", "pose", "scene", "style", "continuity", "source")


def _clean_text(value: Any, limit: int = REFERENCE_ASSET_MAX_TEXT) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()[:limit]


def _clean_list(value: Any, *, limit: int, item_limit: int = 80) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，、/|\s]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _clean_text(item, item_limit)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_reference_asset_scope(value: Any) -> str:
    text = _clean_text(value, 40).lower()
    aliases = {
        "user": "relation_user",
        "member": "relation_user",
        "relation": "relation_user",
        "relation_user": "relation_user",
        "role": "relation_role",
        "relationship": "relation_role",
        "relationship_role": "relation_role",
        "setting_role": "relation_role",
        "relation_role": "relation_role",
        "knowledge": "knowledge",
        "kb": "knowledge",
        "doc": "knowledge",
    }
    return aliases.get(text, "")


def normalize_reference_owner_id(scope: str, value: Any) -> str:
    owner = _clean_text(value, 120)
    if scope == "relation_user":
        return owner
    if scope == "relation_role":
        # Role cards are configuration text, so keep their stable owner key
        # separate from QQ/member identities.  Case folding avoids duplicate
        # assets for English role names while preserving Chinese names.
        if owner.lower().startswith("role:"):
            owner = owner[5:].strip()
        owner = re.sub(r"\s+", " ", owner).strip()
        if not owner or len(owner) > 80 or re.search(r"[\\/\r\n\x00]", owner):
            return ""
        return f"role:{owner.casefold()}"
    if scope == "knowledge":
        if re.fullmatch(r"kb:[A-Za-z0-9_.:-]{1,100}", owner):
            return owner
        if re.fullmatch(r"doc:[A-Za-z0-9_.-]{1,70}:[A-Za-z0-9_.-]{1,70}", owner):
            return owner
    return ""


def default_reference_roles(scope: str) -> list[str]:
    return ["identity"] if scope in {"relation_user", "relation_role"} else ["scene", "style"]


def normalize_reference_asset(
    raw: Any,
    *,
    scope: str | None = None,
    owner_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    clean_scope = normalize_reference_asset_scope(scope or raw.get("scope"))
    raw_owner = owner_id or raw.get("owner_id")
    if clean_scope == "relation_role" and not raw_owner:
        raw_owner = raw.get("role_name") or raw.get("relationship")
    clean_owner = normalize_reference_owner_id(clean_scope, raw_owner)
    if clean_scope not in REFERENCE_ASSET_SCOPES or not clean_owner:
        return None
    source = _clean_text(raw.get("path") or raw.get("source"), 1200)
    if not source:
        return None
    roles = [role for role in _clean_list(raw.get("reference_roles") or raw.get("roles"), limit=7, item_limit=30) if role in REFERENCE_ASSET_ROLES]
    if not roles:
        roles = default_reference_roles(clean_scope)
    timestamp = float(now if now is not None else time.time())
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    return {
        "id": _clean_text(raw.get("id"), 80) or f"asset_{uuid.uuid4().hex}",
        "scope": clean_scope,
        "owner_id": clean_owner,
        "role_name": clean_owner[5:] if clean_scope == "relation_role" and clean_owner.startswith("role:") else "",
        "title": _clean_text(raw.get("title") or raw.get("name"), 120),
        "note": _clean_text(raw.get("note") or raw.get("description"), REFERENCE_ASSET_MAX_TEXT),
        "tags": _clean_list(raw.get("tags"), limit=REFERENCE_ASSET_MAX_TAGS, item_limit=40),
        "path": source,
        "reference_roles": list(dict.fromkeys(roles)),
        "enabled": bool(raw.get("enabled", True)),
        "priority": max(-1000, min(10000, priority)),
        "created_at": float(raw.get("created_at") or timestamp),
        "updated_at": float(raw.get("updated_at") or timestamp),
        "version": REFERENCE_ASSET_VERSION,
    }


def normalize_reference_assets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_owner: dict[tuple[str, str], int] = {}
    for raw in value:
        item = normalize_reference_asset(raw)
        if not item or item["id"] in seen:
            continue
        key = (item["scope"], item["owner_id"])
        if per_owner.get(key, 0) >= REFERENCE_ASSET_MAX_PER_OWNER:
            continue
        seen.add(item["id"])
        per_owner[key] = per_owner.get(key, 0) + 1
        result.append(item)
        if len(result) >= REFERENCE_ASSET_MAX_TOTAL:
            break
    return result


def reference_asset_tokens(asset: dict[str, Any]) -> tuple[str, ...]:
    values = [asset.get("title"), asset.get("note"), *(asset.get("tags") or [])]
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value, 160).lower()
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        for token in (text, compact):
            if len(token) >= 2 and token not in seen:
                seen.add(token)
                tokens.append(token)
    return tuple(tokens)


__all__ = [
    "REFERENCE_ASSET_VERSION",
    "REFERENCE_ASSET_MAX_BYTES",
    "REFERENCE_ASSET_MAX_TOTAL",
    "REFERENCE_ASSET_MAX_PER_OWNER",
    "REFERENCE_ASSET_MAX_TAGS",
    "REFERENCE_ASSET_SCOPES",
    "REFERENCE_ASSET_ROLES",
    "default_reference_roles",
    "normalize_reference_asset_scope",
    "normalize_reference_owner_id",
    "normalize_reference_asset",
    "normalize_reference_assets",
    "reference_asset_tokens",
]
