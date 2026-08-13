# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from typing import Any

from .helpers import _safe_float, _safe_int, _single_line


REACTION_EXPRESSION_STATE_TEMPLATE: dict[str, Any] = {
    "last_sent_at": 0.0,
    "last_intent_signature": "",
    "last_image_id": "",
    "recent_images": [],
    "recent_outcomes": [],
    "preference": {
        "score": 0,
        "positive_count": 0,
        "negative_count": 0,
        "last_feedback_at": 0.0,
    },
    "feedback_events": [],
    # Per-asset feedback is intentionally small and bounded by the recorder.
    # It lets matching prefer a user's known choices without overriding the
    # semantic relevance of the current request.
    "asset_preferences": {},
    "feedback_target": {},
    "feedback_targets": {},
    "reservation": {},
    "pending_images": {},
    "scopes": {},
    # A direct user boundary applies to automatic reaction attachments until
    # the user explicitly asks to resume them. Explicit image requests remain
    # available through the normal tool path.
    "auto_disabled": False,
    "auto_preference_updated_at": 0.0,
    "auto_preference_source": "",
    "auto_disabled_scopes": {},
}

REACTION_EXPRESSION_RESERVATION_SECONDS = 600.0
# Keep enough sent-image history to cover the duplicate window independently
# from the number of lookup phrases configured for a single expression.
REACTION_EXPRESSION_RECENT_IMAGES_MAX = 256
# A full-scale setting is intentionally treated as a distinct mode.  It means
# "do not lose opportunities to model omission", while cooldown, duplicate
# and explicit user boundaries remain active.
REACTION_EXPRESSION_HIGH_FREQUENCY_THRESHOLD = 0.999


def reaction_expression_normalize_probability(
    value: Any,
    default: float = 0.2,
) -> float:
    """Accept both the persisted unit value and a UI-style percentage."""
    raw = _safe_float(value, default, 0.0)
    if raw > 1.0:
        raw /= 100.0
    return max(0.0, min(1.0, raw))


def reaction_expression_high_frequency(value: Any) -> bool:
    """Return whether the configured rate requests omission-resistant delivery."""
    return (
        reaction_expression_normalize_probability(value, 0.0)
        >= REACTION_EXPRESSION_HIGH_FREQUENCY_THRESHOLD
    )


def ensure_reaction_expression_state(user: dict[str, Any]) -> dict[str, Any]:
    raw = user.get("reaction_expression")
    state = raw if isinstance(raw, dict) else {}
    if state is not raw:
        user["reaction_expression"] = state
    for key, default in REACTION_EXPRESSION_STATE_TEMPLATE.items():
        invalid_container = isinstance(default, (dict, list)) and not isinstance(
            state.get(key), type(default)
        )
        if key not in state or invalid_container:
            state[key] = deepcopy(default)
    preference = state["preference"]
    for key, default in REACTION_EXPRESSION_STATE_TEMPLATE["preference"].items():
        preference.setdefault(key, default)
    return state


def reaction_expression_scope_state(
    state: dict[str, Any], scope_key: Any
) -> dict[str, Any]:
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        scopes = {}
        state["scopes"] = scopes
    key = _single_line(scope_key, 240) or "unknown"
    raw = scopes.get(key)
    scoped = raw if isinstance(raw, dict) else {}
    if scoped is not raw:
        scopes[key] = scoped
    scoped.setdefault("last_sent_at", 0.0)
    scoped.setdefault("last_intent_signature", "")
    scoped.setdefault("last_offer_at", 0.0)
    if not isinstance(scoped.get("reservation"), dict):
        scoped["reservation"] = {}
    return scoped


def reaction_expression_effective_probability(
    state: dict[str, Any], configured_probability: Any
) -> float:
    """Apply learned preference as a gentle bias without overriding the configured rate."""
    base = reaction_expression_normalize_probability(configured_probability, 0.2)
    preference = state.get("preference")
    score = (
        _safe_int(preference.get("score"), 0, -20, 20)
        if isinstance(preference, dict)
        else 0
    )
    factor = max(0.35, min(1.35, 1.0 + score * 0.06))
    return max(0.0, min(1.0, base * factor))


def reaction_expression_selection_preferences(
    state: dict[str, Any],
    *,
    intent_signature: Any = "",
    limit: int = 24,
) -> dict[str, Any]:
    """Return compact learned asset affinity data for the current lookup.

    The matcher receives only a bounded snapshot. This keeps persisted user
    state from becoming an unbounded lookup payload while retaining the most
    useful positive/negative choices.
    """
    if not isinstance(state, dict):
        return {}
    raw = state.get("asset_preferences")
    if not isinstance(raw, dict):
        return {}
    signature = _single_line(intent_signature, 40)
    rows: list[dict[str, Any]] = []
    for raw_key, raw_item in raw.items():
        if not isinstance(raw_item, dict):
            continue
        key = _single_line(raw_key, 180)
        if not key:
            continue
        score = _safe_int(raw_item.get("score"), 0, -20, 20)
        positive = _safe_int(raw_item.get("positive_count"), 0, 0, 1000)
        negative = _safe_int(raw_item.get("negative_count"), 0, 0, 1000)
        intent_scores = raw_item.get("intent_scores")
        selected_intent_score = 0
        if signature and isinstance(intent_scores, dict):
            selected_intent_score = _safe_int(intent_scores.get(signature), 0, -8, 8)
        if not score and not selected_intent_score and not positive and not negative:
            continue
        rows.append(
            {
                "key": key,
                "score": score,
                "positive_count": positive,
                "negative_count": negative,
                "intent_score": selected_intent_score,
            }
        )
    if not rows:
        return {}
    rows.sort(
        key=lambda item: (
            abs(_safe_int(item.get("score"), 0, -20, 20))
            + abs(_safe_int(item.get("intent_score"), 0, -8, 8)) * 2,
            _safe_int(item.get("positive_count"), 0, 0),
            _safe_int(item.get("negative_count"), 0, 0),
        ),
        reverse=True,
    )
    return {
        "intent_signature": signature,
        "assets": rows[: max(1, min(_safe_int(limit, 24, 1, 64), 64))],
    }


def _candidate_query_list(value: Any, *, limit: int) -> list[str]:
    raw_items: list[Any]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value or "").strip()
        parsed: Any = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
        raw_items = list(parsed) if isinstance(parsed, list) else re.split(r"[\n;；|]+", text)

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        query = _single_line(item, 160)
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        result.append(query)
        if len(result) >= limit:
            break
    return result


def normalize_reaction_expression_intent(
    *,
    query: Any = "",
    context: Any = "",
    purpose: Any = "",
    emotion: Any = "",
    intensity: Any = 0,
    candidate_queries: Any = "",
    candidate_limit: int = 6,
) -> dict[str, Any]:
    limit = _safe_int(candidate_limit, 6, 1, 16)
    query_text = _single_line(query, 500)
    purpose_text = _single_line(purpose, 120)
    emotion_text = _single_line(emotion, 80)
    context_text = _single_line(context, 1000)
    candidates = _candidate_query_list(candidate_queries, limit=limit)
    if query_text and query_text.casefold() not in {item.casefold() for item in candidates}:
        candidates.insert(0, query_text)
        candidates = candidates[:limit]

    provider_query = query_text or (candidates[0] if candidates else "")
    if not provider_query:
        provider_query = " ".join(part for part in (purpose_text, emotion_text, "表情反应") if part)
    provider_query = _single_line(provider_query or "适合当前语境的表情反应", 500)
    normalized_intensity = _safe_int(intensity, 0, 0, 5)
    signature_source = json.dumps(
        {
            "purpose": purpose_text.casefold(),
            "emotion": emotion_text.casefold(),
            "intensity": normalized_intensity,
            "queries": [item.casefold() for item in candidates] or [provider_query.casefold()],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:24]
    return {
        "purpose": purpose_text,
        "emotion": emotion_text,
        "intensity": normalized_intensity,
        "context": context_text,
        "candidate_queries": candidates,
        "provider_query": provider_query,
        "signature": signature,
    }


def evaluate_reaction_expression_gate(
    state: dict[str, Any],
    intent: dict[str, Any],
    *,
    now: float,
    probability: float,
    cooldown_seconds: float,
    random_value: float,
) -> dict[str, Any]:
    probability = reaction_expression_normalize_probability(probability, 0.2)
    cooldown_seconds = _safe_float(cooldown_seconds, 180.0, 0.0, 86400.0)
    signature = _single_line(intent.get("signature"), 40)

    reservation = state.get("reservation")
    if isinstance(reservation, dict):
        reserved_at = _safe_float(reservation.get("at"), 0.0)
        if (
            reserved_at > 0
            and now - reserved_at < REACTION_EXPRESSION_RESERVATION_SECONDS
        ):
            return {"allowed": False, "reason": "in_progress", "probability": probability}

    last_sent_at = _safe_float(state.get("last_sent_at"), 0.0)
    if last_sent_at > 0 and cooldown_seconds > 0 and now - last_sent_at < cooldown_seconds:
        return {"allowed": False, "reason": "cooldown", "probability": probability}

    if (
        signature
        and signature == _single_line(state.get("last_intent_signature"), 40)
        and last_sent_at > 0
        and now - last_sent_at < max(60.0, cooldown_seconds)
    ):
        return {"allowed": False, "reason": "repeated_intent", "probability": probability}

    if probability <= 0 or (
        probability < 1.0
        and _safe_float(random_value, 1.0, 0.0, 1.0) >= probability
    ):
        return {"allowed": False, "reason": "probability", "probability": probability}
    return {"allowed": True, "reason": "allowed", "probability": probability}


def reserve_reaction_expression_intent(
    state: dict[str, Any],
    intent: dict[str, Any],
    *,
    now: float,
    reservation_token: Any = "",
) -> str:
    signature = _single_line(intent.get("signature"), 40)
    token = _single_line(reservation_token, 80)
    if not token:
        token = hashlib.sha256(
            f"{signature}:{float(now)}:{id(state)}".encode("utf-8")
        ).hexdigest()[:32]
    state["reservation"] = {
        "token": token,
        "signature": signature,
        "at": float(now),
    }
    return token


def reaction_expression_reservation_owned(
    state: dict[str, Any], reservation_token: Any
) -> bool:
    reservation = state.get("reservation")
    if not isinstance(reservation, dict):
        return False
    expected = _single_line(reservation_token, 80)
    current = _single_line(reservation.get("token"), 80)
    return bool(expected and current and expected == current)


def release_reaction_expression_reservation(
    state: dict[str, Any],
    *,
    intent_signature: str = "",
    reservation_token: str = "",
) -> None:
    reservation = state.get("reservation")
    if not isinstance(reservation, dict):
        state["reservation"] = {}
        return
    expected_token = _single_line(reservation_token, 80)
    current_token = _single_line(reservation.get("token"), 80)
    if expected_token:
        if current_token and expected_token == current_token:
            state["reservation"] = {}
        return
    expected_signature = _single_line(intent_signature, 40)
    current_signature = _single_line(reservation.get("signature"), 40)
    if (
        not expected_signature
        or not current_signature
        or expected_signature == current_signature
    ):
        state["reservation"] = {}


def reaction_expression_image_keys(image_id: Any, image_path: Any) -> list[str]:
    keys: list[str] = []
    normalized_id = _single_line(image_id, 160)
    if normalized_id:
        keys.append(f"id:{normalized_id}")
    raw_path = str(image_path or "").strip()
    if raw_path:
        normalized_path = os.path.normcase(os.path.abspath(os.path.normpath(raw_path)))
        normalized_path_key = normalized_path.replace("\\", "/").casefold()
        keys.append(f"path:{normalized_path_key}")
    return keys


def reaction_expression_image_key(image_id: Any, image_path: Any) -> str:
    keys = reaction_expression_image_keys(image_id, image_path)
    return keys[0] if keys else ""


def _normalize_image_keys(value: Any) -> list[str]:
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        key = _single_line(item, 1000)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def reserve_reaction_expression_image(
    state: dict[str, Any],
    *,
    image_key: str = "",
    image_keys: Any = None,
    now: float,
    duplicate_window_seconds: float,
    reservation_token: Any = "",
) -> bool:
    keys = _normalize_image_keys(image_keys)
    for key in _normalize_image_keys(image_key):
        if key not in keys:
            keys.append(key)
    if not keys:
        return False
    key_set = set(keys)
    window = _safe_float(duplicate_window_seconds, 600.0, 60.0, 86400.0 * 7)
    recent_images = state.get("recent_images")
    if not isinstance(recent_images, list):
        recent_images = []
        state["recent_images"] = recent_images
    cutoff = float(now) - window
    recent_images[:] = [
        item
        for item in recent_images
        if not isinstance(item, dict)
        or _safe_float(item.get("sent_at"), 0.0) <= 0
        or _safe_float(item.get("sent_at"), 0.0) >= cutoff
    ]
    if len(recent_images) > REACTION_EXPRESSION_RECENT_IMAGES_MAX:
        recent_images[:] = recent_images[-REACTION_EXPRESSION_RECENT_IMAGES_MAX:]
    for item in recent_images:
        if not isinstance(item, dict):
            continue
        recent_keys = _normalize_image_keys(item.get("keys"))
        for recent_key in _normalize_image_keys(item.get("key")):
            if recent_key not in recent_keys:
                recent_keys.append(recent_key)
        for recent_key in reaction_expression_image_keys(
            item.get("image_id"), item.get("path")
        ):
            if recent_key not in recent_keys:
                recent_keys.append(recent_key)
        if key_set.isdisjoint(recent_keys):
            continue
        sent_at = _safe_float(item.get("sent_at"), 0.0)
        if sent_at <= 0 or now - sent_at < window:
            return False

    pending = state.get("pending_images")
    if not isinstance(pending, dict):
        pending = {}
        state["pending_images"] = pending
    for pending_key, pending_value in list(pending.items()):
        pending_at = (
            pending_value.get("at")
            if isinstance(pending_value, dict)
            else pending_value
        )
        if (
            now - _safe_float(pending_at, 0.0)
            >= REACTION_EXPRESSION_RESERVATION_SECONDS
        ):
            pending.pop(pending_key, None)
    if any(key in pending for key in keys):
        return False
    token = _single_line(reservation_token, 80)
    for key in keys:
        pending[key] = {"at": float(now), "token": token}
    return True


def release_reaction_expression_image(
    state: dict[str, Any],
    image_key: str = "",
    *,
    image_keys: Any = None,
    reservation_token: Any = "",
) -> None:
    pending = state.get("pending_images")
    if isinstance(pending, dict):
        keys = _normalize_image_keys(image_keys)
        for key in _normalize_image_keys(image_key):
            if key not in keys:
                keys.append(key)
        token = _single_line(reservation_token, 80)
        for key in keys:
            if not token:
                pending.pop(key, None)
                continue
            current = pending.get(key)
            if isinstance(current, dict) and _single_line(current.get("token"), 80):
                if _single_line(current.get("token"), 80) == token:
                    pending.pop(key, None)
            elif current is not None:
                # Older state used numeric timestamps and has no ownership token.
                pending.pop(key, None)


def append_reaction_expression_outcome(
    state: dict[str, Any],
    *,
    status: str,
    reason: str,
    intent_signature: str,
    now: float,
    candidate_limit: int,
    image_key: str = "",
    cache_hit: bool | None = None,
    latency_ms: float | None = None,
) -> None:
    outcomes = state.get("recent_outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
        state["recent_outcomes"] = outcomes
    outcome = {
        "at": float(now),
        "status": _single_line(status, 32),
        "reason": _single_line(reason, 120),
        "intent_signature": _single_line(intent_signature, 40),
        "image_key": _single_line(image_key, 1000),
    }
    if cache_hit is not None:
        outcome["cache_hit"] = bool(cache_hit)
    if latency_ms is not None:
        outcome["latency_ms"] = round(
            _safe_float(latency_ms, 0.0, 0.0, 3_600_000.0), 2
        )
    outcomes.append(outcome)
    keep = max(4, _safe_int(candidate_limit, 6, 1, 16) * 2)
    del outcomes[:-keep]


def record_reaction_expression_sent(
    state: dict[str, Any],
    intent: dict[str, Any],
    *,
    image_id: Any,
    image_path: Any,
    image_key: str,
    image_keys: Any = None,
    now: float,
    candidate_limit: int,
    duplicate_window_seconds: float = 600.0,
    scope_key: str = "",
    reservation_token: str = "",
    cache_hit: bool | None = None,
    latency_ms: float | None = None,
) -> None:
    signature = _single_line(intent.get("signature"), 40)
    state["last_sent_at"] = float(now)
    state["last_intent_signature"] = signature
    state["last_image_id"] = _single_line(image_id, 160)
    normalized_image_keys = _normalize_image_keys(image_keys)
    for key in reaction_expression_image_keys(image_id, image_path):
        if key not in normalized_image_keys:
            normalized_image_keys.append(key)
    if image_key and image_key not in normalized_image_keys:
        normalized_image_keys.insert(0, image_key)
    release_reaction_expression_reservation(
        state,
        intent_signature=signature,
        reservation_token=reservation_token,
    )
    release_reaction_expression_image(
        state,
        image_key,
        image_keys=normalized_image_keys,
        reservation_token=reservation_token,
    )
    if scope_key:
        scoped = reaction_expression_scope_state(state, scope_key)
        scoped["last_sent_at"] = float(now)
        scoped["last_intent_signature"] = signature
        release_reaction_expression_reservation(
            scoped,
            intent_signature=signature,
            reservation_token=reservation_token,
        )

    recent_images = state.get("recent_images")
    if not isinstance(recent_images, list):
        recent_images = []
        state["recent_images"] = recent_images
    recent_images.append(
        {
            "key": _single_line(image_key, 1000),
            "keys": normalized_image_keys,
            "image_id": _single_line(image_id, 160),
            "path": _single_line(image_path, 1000),
            "sent_at": float(now),
            "intent_signature": signature,
        }
    )
    # Candidate-limit controls lookup breadth, not how long duplicate
    # protection remembers delivered images. Prune by the actual duplicate
    # window and use a fixed defensive cap for malformed/high-volume state.
    window = _safe_float(
        duplicate_window_seconds,
        600.0,
        60.0,
        86400.0 * 7,
    )
    cutoff = float(now) - window
    recent_images[:] = [
        item
        for item in recent_images
        if not isinstance(item, dict)
        or _safe_float(item.get("sent_at"), 0.0) <= 0
        or _safe_float(item.get("sent_at"), 0.0) >= cutoff
    ]
    if len(recent_images) > REACTION_EXPRESSION_RECENT_IMAGES_MAX:
        recent_images[:] = recent_images[-REACTION_EXPRESSION_RECENT_IMAGES_MAX:]
    feedback_target = {
        "image_key": _single_line(image_key, 1000),
        "image_id": _single_line(image_id, 160),
        "path": _single_line(image_path, 1000),
        "intent_signature": signature,
        "sent_at": float(now),
        "expires_at": float(now) + 6 * 3600,
    }
    state["feedback_target"] = feedback_target
    normalized_scope_key = _single_line(scope_key, 240)
    if normalized_scope_key:
        feedback_targets = state.get("feedback_targets")
        if not isinstance(feedback_targets, dict):
            feedback_targets = {}
            state["feedback_targets"] = feedback_targets
        feedback_targets[normalized_scope_key] = dict(feedback_target)
    append_reaction_expression_outcome(
        state,
        status="sent",
        reason="delivered",
        intent_signature=signature,
        now=now,
        candidate_limit=candidate_limit,
        image_key=image_key,
        cache_hit=cache_hit,
        latency_ms=latency_ms,
    )


_NEGATIVE_FEEDBACK_PATTERNS = (
    r"(?:别|不要|别再|不用再).{0,8}(?:发|用).{0,6}(?:表情包|反应图|这种图)",
    r"(?:刚才|上一张|那张|这个).{0,10}(?:表情包|反应图|图)?.{0,8}(?:不合适|不贴切|不喜欢|很尴尬|太尴尬|难看)",
    r"(?:表情包|反应图|这张图).{0,8}(?:不合适|不贴切|不喜欢|很尴尬|太尴尬|难看)",
    r"^(?:别发了|别用了|不要这种|不想看(?:表情包|表情|反应图)?|先别发)$",
)
_POSITIVE_FEEDBACK_PATTERNS = (
    r"(?:刚才|上一张|那张|这个|这张).{0,10}(?:表情包|反应图|图)?.{0,8}(?:不错|合适|贴切|喜欢|好笑|好用)",
    r"(?:表情包|反应图|这张图).{0,8}(?:不错|合适|贴切|喜欢|好笑|好用)",
    r"^(?:太贴了|太适合了|这个可以|好好笑|笑死了?|绝了|这张可以)$",
)

_EXPLICIT_REACTION_OPT_OUT_PATTERNS = (
    r"(?:别|不要|别再|不用再).{0,8}(?:发|用).{0,8}(?:表情包|表情|反应图|贴纸|梗图|斗图|这种图)",
    r"(?:不想看|不需要|关闭|停用).{0,6}(?:表情包|表情|反应图|贴纸|梗图|斗图)",
    r"(?:表情包|反应图|贴纸|梗图|斗图|这种图).{0,6}(?:以后)?(?:别|不要|别再|不用再).{0,4}(?:发|用|来)",
    r"^(?:别发了|别用了|先别发|不要这种)$",
)

_EXPLICIT_REACTION_OPT_IN_PATTERNS = (
    r"(?:可以|能)?继续(?:自动)?(?:发|用)(?:表情包|表情|反应图|贴纸|这种图)(?:了|吧)?",
    r"(?:恢复|重新开启|重新打开|重新启用).{0,6}(?:自动)?(?:表情包|表情|反应图|贴纸)(?:自动|功能|发送)?",
    r"(?:打开|开启|启用|解除停用|取消停用).{0,6}(?:自动)?(?:表情包|表情|反应图|贴纸)(?:自动|功能|发送)?",
    r"(?:不用停|不用关闭|别停|恢复默认).{0,8}(?:表情包|表情|反应图|贴纸)",
)


def reaction_expression_explicit_opt_out(text: Any) -> bool:
    """Detect a direct request to stop automatic reaction images.

    This is deliberately narrow: only an explicit request about reaction
    images blocks the current offer. Ordinary negative sentiment remains a
    learned feedback signal instead of becoming a hard gate.
    """
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return False
    return any(
        re.search(pattern, compact, flags=re.I)
        for pattern in _EXPLICIT_REACTION_OPT_OUT_PATTERNS
    )


def reaction_expression_explicit_opt_in(text: Any) -> bool:
    """Detect a direct request to resume automatic reaction images."""
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return False
    return any(
        re.search(pattern, compact, flags=re.I)
        for pattern in _EXPLICIT_REACTION_OPT_IN_PATTERNS
    )


_REACTION_EXPRESSION_ASSET_TERM = r"(?:表情包|表情|反应图|贴纸|梗图|斗图)"
_REACTION_EXPRESSION_REQUEST_VERB = r"(?:来|发|找|用|整|搞)"
_EXPLICIT_REACTION_REQUEST_PATTERNS = (
    rf"(?:请|麻烦|拜托|帮我|给我|替我|能不能|可不可以|可以|能否|这次|这回|现在|马上|立刻|赶紧|要不|不如|重新|再|继续).{{0,8}}{_REACTION_EXPRESSION_REQUEST_VERB}(?!了|过|的|来的).{{0,10}}{_REACTION_EXPRESSION_ASSET_TERM}",
    rf"^(?:你)?(?:给我)?{_REACTION_EXPRESSION_REQUEST_VERB}(?!了|过|的|来的)(?:个|一个|一张|张|点|些)?.{{0,10}}{_REACTION_EXPRESSION_ASSET_TERM}(?:吧|呗|呀|啊|嘛|吗|看看)?$",
    rf"^(?:我)?(?:想看|要看|想要|需要|我要).{{0,8}}{_REACTION_EXPRESSION_ASSET_TERM}(?:吧|呗|呀|啊|嘛|吗)?$",
    rf"^要(?:个|一个|一张|张|点|些).{{0,8}}{_REACTION_EXPRESSION_ASSET_TERM}(?:吧|呗|呀|啊|嘛|吗)?$",
    rf"{_REACTION_EXPRESSION_ASSET_TERM}.{{0,6}}(?:给我)?(?:来|发|找|整|搞)(?!了|过|的|来的)(?:个|一个|一张|张|点|些)?(?:吧|呗|呀|啊|嘛|吗)?$",
    rf"(?:请|帮我|给我|直接)?(?:用|拿)(?!了|过|的|来的).{{0,8}}{_REACTION_EXPRESSION_ASSET_TERM}.{{0,8}}(?:回复|回应|回|接)(?:一下)?(?:吧|呗|呀|啊|嘛|吗)?$",
)
_HISTORICAL_REACTION_MENTION_PATTERNS = (
    rf"(?:刚才|刚刚|之前|上次|此前|先前|前面|过去|已经|曾经|方才|为什么|怎么还|怎么又).{{0,24}}{_REACTION_EXPRESSION_ASSET_TERM}",
    rf"(?:你|他|她|它).{{0,6}}(?:发|用|来|找|整|搞)(?:了|过|的|来的).{{0,12}}{_REACTION_EXPRESSION_ASSET_TERM}",
)


def reaction_expression_explicit_request(text: Any) -> bool:
    """Return whether the current user message explicitly asks for an image."""
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return False
    clauses = [
        part
        for part in re.split(
            r"(?:[，,。！？!?；;]+|但是|不过|然而|然后|可是|但|只是)", compact
        )
        if part
    ] or [compact]
    for clause in clauses:
        if reaction_expression_explicit_opt_out(clause):
            continue
        if any(
            re.search(pattern, clause, flags=re.I)
            for pattern in _HISTORICAL_REACTION_MENTION_PATTERNS
        ):
            continue
        if any(
            re.search(pattern, clause, flags=re.I)
            for pattern in _EXPLICIT_REACTION_REQUEST_PATTERNS
        ):
            return True
    return False


def reaction_expression_auto_disabled(state: dict[str, Any], scope_key: Any = "") -> bool:
    """Read the per-conversation automatic reaction boundary."""
    if not isinstance(state, dict):
        return False
    key = _single_line(scope_key, 240)
    scoped = state.get("auto_disabled_scopes")
    if key and isinstance(scoped, dict) and key in scoped:
        return bool(scoped.get(key))
    # Compatibility for state written before per-scope preferences existed.
    return bool(state.get("auto_disabled"))


def sync_reaction_expression_auto_preference(
    state: dict[str, Any], text: Any, *, now: float, scope_key: Any = ""
) -> str:
    """Persist an explicit automatic-image boundary for future turns.

    Returns ``disabled``, ``enabled`` when the preference changed and an empty
    string when the message does not express a preference. The state is kept
    on the user record so it survives the current event and process restart.
    """
    if not isinstance(state, dict):
        return ""
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return ""
    key = _single_line(scope_key, 240)
    scoped = state.get("auto_disabled_scopes")
    if not isinstance(scoped, dict):
        scoped = {}
        state["auto_disabled_scopes"] = scoped
    current = reaction_expression_auto_disabled(state, key)
    if reaction_expression_explicit_opt_out(compact):
        mentions_reaction = bool(
            re.search(r"(?:表情包|表情|反应图|贴纸|这种图)", compact, flags=re.I)
        )
        feedback_target = _reaction_expression_feedback_target(state, key)
        feedback_expires_at = (
            _safe_float(feedback_target.get("expires_at"), 0.0)
            if isinstance(feedback_target, dict)
            else 0.0
        )
        has_recent_target = bool(feedback_target) and (
            feedback_expires_at <= 0 or float(now) <= feedback_expires_at
        )
        if not mentions_reaction and not has_recent_target:
            return ""
        if current:
            return ""
        if key:
            scoped[key] = True
        else:
            state["auto_disabled"] = True
        state["auto_preference_updated_at"] = float(now)
        state["auto_preference_source"] = "user_opt_out"
        return "disabled"
    if reaction_expression_explicit_opt_in(compact):
        if not current:
            return ""
        if key:
            scoped[key] = False
        else:
            state["auto_disabled"] = False
        state["auto_preference_updated_at"] = float(now)
        state["auto_preference_source"] = "user_opt_in"
        return "enabled"
    return ""


def _reaction_expression_feedback_target(
    state: dict[str, Any], scope_key: Any = ""
) -> dict[str, Any]:
    normalized_scope_key = _single_line(scope_key, 240)
    feedback_targets = state.get("feedback_targets")
    if normalized_scope_key and isinstance(feedback_targets, dict):
        if normalized_scope_key in feedback_targets:
            target = feedback_targets.get(normalized_scope_key)
            return target if isinstance(target, dict) else {}
        # Once scoped targets exist, a missing scope must not fall back to the
        # legacy global target from another conversation. An empty mapping is
        # retained for old state files that only have feedback_target.
        if feedback_targets:
            return {}
    target = state.get("feedback_target")
    return target if isinstance(target, dict) else {}


def classify_reaction_expression_feedback(
    state: dict[str, Any], text: Any, *, now: float, scope_key: str = ""
) -> str:
    target = _reaction_expression_feedback_target(state, scope_key)
    if not isinstance(target, dict) or not target:
        return ""
    expires_at = _safe_float(target.get("expires_at"), 0.0)
    if expires_at > 0 and now > expires_at:
        return ""
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return ""
    if any(re.search(pattern, compact, flags=re.I) for pattern in _NEGATIVE_FEEDBACK_PATTERNS):
        return "negative"
    if any(re.search(pattern, compact, flags=re.I) for pattern in _POSITIVE_FEEDBACK_PATTERNS):
        return "positive"
    return ""


def record_reaction_expression_feedback(
    state: dict[str, Any],
    signal: Any,
    text: Any,
    *,
    now: float,
    event_limit: int = 12,
    scope_key: str = "",
) -> dict[str, Any]:
    normalized_signal = _single_line(signal, 16).casefold()
    if normalized_signal not in {"positive", "negative"}:
        return {}
    target = _reaction_expression_feedback_target(state, scope_key)
    if not isinstance(target, dict) or not target:
        return {}

    preference = state.get("preference")
    if not isinstance(preference, dict):
        preference = deepcopy(REACTION_EXPRESSION_STATE_TEMPLATE["preference"])
        state["preference"] = preference
    delta = 1 if normalized_signal == "positive" else -1
    preference["score"] = _safe_int(preference.get("score"), 0, -20, 20) + delta
    preference["score"] = max(-20, min(20, preference["score"]))
    count_key = "positive_count" if normalized_signal == "positive" else "negative_count"
    preference[count_key] = _safe_int(preference.get(count_key), 0, 0) + 1
    preference["last_feedback_at"] = float(now)

    asset_key = _single_line(target.get("image_id") or target.get("image_key"), 180)
    target_signature = _single_line(target.get("intent_signature"), 40)
    asset_preferences = state.get("asset_preferences")
    if not isinstance(asset_preferences, dict):
        asset_preferences = {}
        state["asset_preferences"] = asset_preferences
    if asset_key:
        asset_preference = asset_preferences.get(asset_key)
        if not isinstance(asset_preference, dict):
            asset_preference = {}
            asset_preferences[asset_key] = asset_preference
        asset_preference["score"] = max(
            -20,
            min(20, _safe_int(asset_preference.get("score"), 0, -20, 20) + delta),
        )
        asset_preference[count_key] = _safe_int(
            asset_preference.get(count_key), 0, 0
        ) + 1
        asset_preference["last_feedback_at"] = float(now)
        intent_scores = asset_preference.get("intent_scores")
        if not isinstance(intent_scores, dict):
            intent_scores = {}
            asset_preference["intent_scores"] = intent_scores
        if target_signature:
            intent_scores[target_signature] = max(
                -8,
                min(8, _safe_int(intent_scores.get(target_signature), 0, -8, 8) + delta),
            )
            if len(intent_scores) > 24:
                recent_signatures = sorted(
                    intent_scores,
                    key=lambda value: abs(_safe_int(intent_scores.get(value), 0, -8, 8)),
                    reverse=True,
                )[:24]
                asset_preference["intent_scores"] = {
                    value: intent_scores[value] for value in recent_signatures
                }
        if len(asset_preferences) > 64:
            keep_keys = sorted(
                asset_preferences,
                key=lambda value: _safe_float(
                    asset_preferences.get(value, {}).get("last_feedback_at")
                    if isinstance(asset_preferences.get(value), dict)
                    else 0,
                    0.0,
                ),
                reverse=True,
            )[:64]
            state["asset_preferences"] = {
                value: asset_preferences[value] for value in keep_keys
            }

    events = state.get("feedback_events")
    if not isinstance(events, list):
        events = []
        state["feedback_events"] = events
    event = {
        "at": float(now),
        "signal": normalized_signal,
        "text": _single_line(text, 180),
        "image_key": _single_line(target.get("image_key"), 1000),
        "image_id": _single_line(target.get("image_id"), 160),
        "intent_signature": _single_line(target.get("intent_signature"), 40),
    }
    events.append(event)
    keep = _safe_int(event_limit, 12, 4, 32)
    del events[:-keep]
    normalized_scope_key = _single_line(scope_key, 240)
    feedback_targets = state.get("feedback_targets")
    if normalized_scope_key and isinstance(feedback_targets, dict):
        feedback_targets.pop(normalized_scope_key, None)
    legacy_target = state.get("feedback_target")
    if not normalized_scope_key or legacy_target is target or (
        isinstance(legacy_target, dict)
        and _single_line(legacy_target.get("image_key"), 1000) == event["image_key"]
        and _safe_float(legacy_target.get("sent_at"), 0.0)
        == _safe_float(target.get("sent_at"), 0.0)
    ):
        state["feedback_target"] = {}
    return {
        "signal": normalized_signal,
        "score": preference["score"],
        "image_id": event["image_id"],
        "image_key": event["image_key"],
    }
