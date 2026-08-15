# -*- coding: utf-8 -*-
"""Deterministic environment-to-wardrobe policy used before prompt compilation.

The policy only consumes facts already available to the companion.  It does
not perform network I/O and it never invents weather or location data.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping


_SELFIE_KINDS = {"selfie", "portrait", "自拍", "人像"}
_HOME_MARKERS = (
    "在家", "家里", "家中", "居家", "卧室", "客厅", "房间", "床上", "被窝",
    "home", "bedroom", "living room",
)
_SLEEP_MARKERS = (
    "准备睡", "要睡", "睡前", "入睡", "晚安", "洗漱", "上床", "被窝", "刚醒",
    "bedtime", "preparing for sleep", "going to bed", "sleeping", "just woke",
)
_SPORT_MARKERS = ("运动", "跑步", "健身", "体育", "瑜伽", "workout", "gym", "running", "yoga")


@dataclass(frozen=True, slots=True)
class TemperatureFacts:
    temperature_c: float | None = None
    feels_like_c: float | None = None

    @property
    def effective_c(self) -> float | None:
        return self.feels_like_c if self.feels_like_c is not None else self.temperature_c

    @property
    def thermal_level(self) -> str:
        value = self.effective_c
        if value is None:
            return "unknown"
        if value >= 28:
            return "hot"
        if value >= 24:
            return "warm"
        if value >= 13:
            return "mild"
        if value >= 5:
            return "cool"
        return "cold"


@dataclass(frozen=True, slots=True)
class AmbientWardrobePolicy:
    category: str = ""
    source: str = "none"
    rule_id: str = "none"
    thermal_level: str = "unknown"
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    reason: str = ""


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_temperature_facts(value: Any) -> TemperatureFacts:
    """Extract current/feels-like Celsius values from provider mappings or text."""
    if isinstance(value, Mapping):
        current = next(
            (_number(value.get(key)) for key in (
                "temperature_c", "temperature", "temp", "temp_c", "now_temp",
            ) if _number(value.get(key)) is not None),
            None,
        )
        feels = next(
            (_number(value.get(key)) for key in (
                "feels_like_c", "feels_like", "feelsLike", "feelslike", "apparent_temperature",
            ) if _number(value.get(key)) is not None),
            None,
        )
        text = " ".join(
            str(value.get(key) or "")
            for key in ("text", "prompt", "summary", "description")
        )
        parsed = extract_temperature_facts(text)
        return TemperatureFacts(
            temperature_c=current if current is not None else parsed.temperature_c,
            feels_like_c=feels if feels is not None else parsed.feels_like_c,
        )

    text = " ".join(str(value or "").split())
    feels_match = re.search(
        r"(?:体感(?:温度)?|feels[\s_-]*like|apparent(?:[\s_-]*temperature)?)\s*[：:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:°\s*[cC]|℃|度)?",
        text,
        flags=re.I,
    )
    current_match = re.search(
        r"(?:当前(?:温度)?|实时(?:温度)?|气温|温度|temperature|temp)\s*[：:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:°\s*[cC]|℃|度)?",
        text,
        flags=re.I,
    )
    if not current_match:
        current_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*[cC]|℃|摄氏度)", text, flags=re.I)
    return TemperatureFacts(
        temperature_c=_number(current_match.group(1)) if current_match else None,
        feels_like_c=_number(feels_match.group(1)) if feels_match else None,
    )


def _hour_from_context(text: str) -> int | None:
    match = re.search(r"(?:时间|local[_\s-]*time)?\s*[：:]?\s*(\d{1,2}):\d{2}", text, flags=re.I)
    if not match:
        return None
    hour = int(match.group(1))
    return hour if 0 <= hour <= 23 else None


def infer_ambient_wardrobe_policy(
    *,
    workflow_kind: str,
    scene_context: str,
    weather: Any = "",
) -> AmbientWardrobePolicy:
    if str(workflow_kind or "").strip().lower() not in _SELFIE_KINDS:
        return AmbientWardrobePolicy()

    text = " ".join(str(scene_context or "").split()).lower()
    facts = extract_temperature_facts(weather or text)
    thermal = facts.thermal_level
    at_home = any(marker in text for marker in _HOME_MARKERS)
    sleep_signal = any(marker in text for marker in _SLEEP_MARKERS)
    hour = _hour_from_context(text)
    late_night_home = at_home and hour is not None and (hour >= 22 or hour < 5)

    category = ""
    rule_id = "none"
    reason = ""
    if sleep_signal or late_night_home:
        category = "sleepwear"
        rule_id = "ambient_sleep_phase"
        reason = "current sleep phase or late-night home context"
    elif at_home:
        category = "homewear"
        rule_id = "ambient_home_location"
        reason = "current location is home"
    elif any(marker in text for marker in _SPORT_MARKERS):
        category = "sportswear"
        rule_id = "ambient_current_activity"
        reason = "current activity is exercise"

    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    if thermal == "hot":
        required = ("lightweight breathable fabric", "summer-appropriate outfit")
        forbidden = ("wool sweater", "thick knitwear", "hoodie", "heavy coat", "winter layering")
    elif thermal in {"cool", "cold"}:
        required = ("weather-appropriate warm layer",)
        forbidden = ("heatwave styling",)

    return AmbientWardrobePolicy(
        category=category,
        source="ambient_context" if category or thermal != "unknown" else "none",
        rule_id=rule_id if category else ("ambient_temperature" if thermal != "unknown" else "none"),
        thermal_level=thermal,
        required=required,
        forbidden=forbidden,
        reason=reason or ("numeric current or feels-like temperature" if thermal != "unknown" else ""),
    )


def apply_ambient_wardrobe_intent(intent: Any, policy: AmbientWardrobePolicy) -> Any:
    """Return a dataclass copy only when ambient context may choose clothing."""
    from dataclasses import replace

    if not policy.category or getattr(intent, "target_category", ""):
        return intent
    excluded = set(getattr(intent, "excluded_categories", ()) or ())
    if policy.category in excluded:
        return intent
    return replace(
        intent,
        target_category=policy.category,
        target_text=policy.reason,
        source=policy.source,
    )


def character_identity_appearance_from_persona(persona: str, recognition: str = "") -> tuple[str, ...]:
    """Extract immutable identity traits without leaking wardrobe into identity."""
    labels = {
        "性别": "gender", "识别点": "key visual traits", "主要识别点": "key visual traits",
        "外貌": "appearance", "发型发色": "hairstyle and hair color", "发色": "hair color",
        "发型": "hairstyle", "瞳色": "eye color", "眼睛": "eyes",
    }
    values: list[str] = []
    for line in str(persona or "").replace("\r", "\n").split("\n"):
        text = line.strip()
        if not text or ("：" not in text and ":" not in text):
            continue
        label, value = text.split("：", 1) if "：" in text else text.split(":", 1)
        normalized = " ".join(value.split())[:300]
        if label.strip() in labels and normalized:
            values.append(f"{labels[label.strip()]}: {normalized}")
    recognition_text = " ".join(str(recognition or "").split())[:300]
    if recognition_text:
        values.append(f"additional visual recognition notes: {recognition_text}")
    return tuple(dict.fromkeys(values))


def outfit_context_fingerprint(
    *,
    daypart: str,
    location_type: str,
    current_activity: str,
    thermal_level: str,
    route_key: str,
) -> str:
    normalized = "|".join(
        " ".join(str(value or "").lower().split())
        for value in (daypart, location_type, current_activity, thermal_level, route_key)
    )
    return hashlib.sha256(normalized.encode("utf-8", "ignore")).hexdigest()


def hot_outfit_fields(scene: str, index: int = 0) -> dict[str, str]:
    tops = {
        "school": ("breathable short-sleeve school shirt", "light cotton polo shirt"),
        "commute": ("airy short-sleeve blouse or shirt", "lightweight breathable polo top"),
        "sport": ("quick-dry short-sleeve training tee", "breathable sleeveless sports top"),
        "home": ("loose breathable cotton lounge tee", "lightweight short-sleeve home top"),
        "daily": ("light cotton short-sleeve tee", "airy short-sleeve shirt"),
    }
    bottoms = {
        "school": "lightweight straight trousers or school-appropriate knee-length bottoms",
        "commute": "lightweight relaxed trousers",
        "sport": "breathable athletic shorts or lightweight track pants",
        "home": "light cotton lounge shorts or thin relaxed pants",
        "daily": "lightweight shorts or breathable relaxed trousers",
    }
    accessories = {
        "school": "simple hair clip and lightweight school bag",
        "commute": "minimal watch and lightweight tote",
        "sport": "sports watch and water bottle",
        "home": "simple hair band or cool indoor slippers",
        "daily": "small summer crossbody bag or simple bracelet",
    }
    normalized = scene if scene in tops else "daily"
    return {
        "silhouette": "light breathable summer silhouette",
        "top": tops[normalized][index % len(tops[normalized])],
        "bottom": bottoms[normalized],
        "accessory": accessories[normalized],
    }
