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

try:
    from .generation_contracts import OutfitPieceV2, OutfitSpecV2
except ImportError:  # pragma: no cover
    from generation_contracts import OutfitPieceV2, OutfitSpecV2


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
        "school": ("lightweight straight trousers", "school-appropriate knee-length pleated skirt"),
        "commute": ("lightweight relaxed trousers",),
        "sport": ("breathable athletic shorts", "lightweight track pants"),
        "home": ("light cotton lounge shorts", "thin relaxed pants"),
        "daily": ("lightweight shorts", "breathable relaxed trousers"),
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
        "bottom": bottoms[normalized][index % len(bottoms[normalized])],
        "accessory": accessories[normalized],
    }


DEFAULT_CANONICAL_OUTFIT_NEGATIVES = (
    "official outfit", "canonical outfit", "power suit", "combat suit", "bodysuit", "leotard",
    "armored collar", "detached collar", "chest jewel", "chest ornament", "heart-shaped gem",
    "holographic bracelet", "waist cape", "showgirl skirt", "single thighhigh",
    "asymmetrical sleeves", "futuristic uniform",
)

_CANONICAL_REQUEST = re.compile(
    r"官方(?:服装|衣服|造型|作战服)|原版(?:服装|衣服|造型)|默认(?:服装|衣服|造型)|作战服|"
    r"\b(?:official|canonical|default)\s+(?:outfit|costume|uniform)\b|\bpower\s+suit\b",
    flags=re.I,
)


def infer_outfit_mode(request_text: str, *, has_outfit_reference: bool = False) -> str:
    if _CANONICAL_REQUEST.search(str(request_text or "")):
        return "canonical_outfit"
    if has_outfit_reference:
        return "reference_outfit"
    return "free_outfit"


def _piece(**values: str) -> OutfitPieceV2:
    return OutfitPieceV2(**values)


def _catalog(category: str, thermal: str) -> tuple[dict[str, Any], ...]:
    hot = thermal in {"hot", "warm"}
    entries: dict[str, tuple[dict[str, Any], ...]] = {
        "homewear": (
            {"top": _piece(kind="oversized t-shirt", color="mint green", material="soft cotton", fit="loose fit", neckline="plain crew neck", sleeves="short sleeves"), "bottom": _piece(kind="lounge shorts", color="light gray", material="lightweight cotton", fit="relaxed fit"), "legwear": "bare legs", "footwear": "white indoor slippers", "outerwear": "no jacket"},
            {"top": _piece(kind="relaxed t-shirt", color="pale blue", material="breathable cotton", fit="loose fit", neckline="simple round neck", sleeves="short sleeves"), "bottom": _piece(kind="drawstring lounge shorts", color="cream", material="thin cotton", fit="relaxed fit"), "legwear": "bare legs", "footwear": "soft house slippers", "outerwear": "no jacket"},
        ) if hot else (
            {"top": _piece(kind="soft sweatshirt", color="sage green", material="cotton fleece", fit="relaxed fit", neckline="plain crew neck", sleeves="long sleeves"), "bottom": _piece(kind="straight lounge pants", color="light gray", material="soft cotton", fit="relaxed fit"), "legwear": "bare ankles", "footwear": "soft house slippers", "outerwear": "no jacket"},
        ),
        "sleepwear": (
            {"top": _piece(kind="pajama shirt", color="pale blue", material="lightweight cotton", fit="loose fit", neckline="simple fold-down collar", sleeves="short sleeves"), "bottom": _piece(kind="matching pajama shorts", color="pale blue", material="lightweight cotton", fit="loose fit"), "legwear": "bare legs", "footwear": "soft house slippers", "outerwear": "no outerwear"},
            {"top": _piece(kind="sleep t-shirt", color="soft lavender", material="breathable cotton", fit="oversized fit", neckline="plain crew neck", sleeves="short sleeves"), "bottom": _piece(kind="plain sleep shorts", color="white", material="light cotton", fit="relaxed fit"), "legwear": "bare legs", "footwear": "bare feet", "outerwear": "no outerwear"},
        ) if hot else (
            {"top": _piece(kind="pajama shirt", color="pale blue", material="soft brushed cotton", fit="loose fit", neckline="simple fold-down collar", sleeves="long sleeves"), "bottom": _piece(kind="matching pajama pants", color="pale blue", material="soft brushed cotton", fit="loose fit"), "legwear": "covered legs", "footwear": "soft house slippers", "outerwear": "no outerwear"},
        ),
        "sportswear": (
            {"top": _piece(kind="training t-shirt", color="white", material="quick-dry fabric", fit="athletic fit", neckline="crew neck", sleeves="short sleeves"), "bottom": _piece(kind="running shorts", color="navy blue", material="breathable performance fabric", fit="athletic fit"), "legwear": "bare legs", "footwear": "white running shoes", "outerwear": "no jacket"},
        ),
        "school_uniform": (
            {"top": _piece(kind="school shirt", color="white", material="light cotton", fit="neat fit", neckline="point collar", sleeves="short sleeves"), "bottom": _piece(kind="pleated skirt", color="navy blue", material="light woven fabric", fit="knee-length fit"), "legwear": "plain ankle socks", "footwear": "black loafers", "outerwear": "no blazer"},
        ),
        "formalwear": (
            {"topology": "one_piece", "one_piece": _piece(kind="simple midi dress", color="deep navy", material="matte satin", fit="tailored fit", neckline="modest square neck", sleeves="short sleeves"), "legwear": "bare legs", "footwear": "low-heel pumps", "outerwear": "no jacket"},
        ),
        "swimwear": (
            {"topology": "one_piece", "one_piece": _piece(kind="one-piece swimsuit", color="teal blue", material="smooth swim fabric", fit="clean athletic fit", neckline="simple scoop neck", sleeves="sleeveless"), "legwear": "bare legs", "footwear": "poolside sandals", "outerwear": "no outerwear"},
        ),
        "daily_outfit": (
            {"top": _piece(kind="plain t-shirt", color="white", material="breathable cotton", fit="relaxed fit", neckline="crew neck", sleeves="short sleeves"), "bottom": _piece(kind="pleated skirt", color="sky blue", material="lightweight woven fabric", fit="above-knee fit"), "legwear": "bare legs", "footwear": "white low-top sneakers", "outerwear": "no jacket"},
            {"topology": "one_piece", "one_piece": _piece(kind="simple summer dress", color="mint green", material="light cotton", fit="relaxed waist fit", neckline="plain round neck", sleeves="short sleeves"), "legwear": "bare legs", "footwear": "white flat shoes", "outerwear": "no jacket"},
        ) if hot else (
            {"top": _piece(kind="fine-gauge cardigan", color="cream", material="light knit cotton", fit="relaxed fit", neckline="v-neck", sleeves="long sleeves"), "bottom": _piece(kind="straight trousers", color="charcoal gray", material="soft twill", fit="straight fit"), "legwear": "covered legs", "footwear": "black loafers", "outerwear": "single cardigan layer"},
        ),
    }
    return entries.get(category) or entries["daily_outfit"]


def _specific_outfit_items(request_text: str) -> tuple[str, ...]:
    text = " ".join(str(request_text or "").split())
    garment = re.compile(
        r"[^,，;；。]{0,60}(?:hoodie|t-?shirt|shirt|blouse|shorts|trousers|pants|skirt|dress|"
        r"sneakers|slippers|pajama|pyjama|sweater|cardigan|jacket|coat|uniform|sportswear|bodysuit|leotard|armor|armour|"
        r"连帽衫|卫衣|T恤|衬衫|短裤|长裤|裤子|短裙|长裙|连衣裙|运动鞋|拖鞋|睡衣|毛衣|针织衫|外套|羽绒服|制服)"
        r"[^,，;；。]{0,40}",
        flags=re.I,
    )
    abstract_sleepwear = re.compile(r"(?:sleepwear|nightwear|pajamas?|pyjamas?|睡衣)", flags=re.I)
    concrete_detail = re.compile(
        r"(?:black|white|blue|green|pink|purple|lavender|gray|grey|cream|cotton|silk|satin|linen|wool|"
        r"short[- ]sleeve|long[- ]sleeve|sleeveless|oversized|loose|fitted|button|collar|shorts|pants|"
        r"黑|白|蓝|绿|粉|紫|灰|米色|棉|真丝|丝绸|缎|亚麻|羊毛|短袖|长袖|无袖|宽松|修身|纽扣|领|短裤|长裤)",
        flags=re.I,
    )
    items: list[str] = []
    for match in garment.finditer(text):
        item = " ".join(match.group(0).split()).strip()
        # “看看睡衣 / sleepwear”仍是抽象类别，不能冒充已给出的具体服装结构。
        if abstract_sleepwear.search(item) and not concrete_detail.search(item):
            continue
        if item and item not in items:
            items.append(item)
    return tuple(items[:8])


def resolve_structured_outfit(
    *,
    category: str,
    thermal_level: str,
    context_key: str,
    request_text: str = "",
    has_outfit_reference: bool = False,
    canonical_forbidden: tuple[str, ...] = DEFAULT_CANONICAL_OUTFIT_NEGATIVES,
) -> OutfitSpecV2:
    mode = infer_outfit_mode(request_text, has_outfit_reference=has_outfit_reference)
    normalized_category = str(category or "daily_outfit").strip().lower()
    explicit = _specific_outfit_items(request_text)
    templates = _catalog(normalized_category, thermal_level)
    digest = hashlib.sha256(str(context_key or request_text or normalized_category).encode("utf-8", "ignore")).digest()
    selected = dict(templates[int.from_bytes(digest[:2], "big") % len(templates)])
    if mode == "canonical_outfit":
        selected = {
            "explicit_items": explicit or ("official canonical outfit exactly as requested",),
            "top": OutfitPieceV2(), "bottom": OutfitPieceV2(), "one_piece": OutfitPieceV2(),
            "outerwear": "", "material": "", "fit": "", "layering": "one canonical outfit",
        }
    elif mode == "reference_outfit":
        selected = {
            "explicit_items": ("complete coherent outfit from the submitted outfit reference image",),
            "top": OutfitPieceV2(), "bottom": OutfitPieceV2(), "one_piece": OutfitPieceV2(),
            "outerwear": "", "material": "", "fit": "", "layering": "preserve referenced layering",
        }
    elif explicit:
        selected = {
            "explicit_items": explicit,
            "top": OutfitPieceV2(), "bottom": OutfitPieceV2(), "one_piece": OutfitPieceV2(),
            "outerwear": "no additional outerwear" if not re.search(r"jacket|coat|hoodie|外套|羽绒服|连帽衫|卫衣", request_text, re.I) else "",
            "material": "materials exactly as requested", "fit": "fit exactly as requested",
            "layering": "single coherent layering",
        }
    selected.setdefault("topology", "separates")
    selected.setdefault("top", OutfitPieceV2())
    selected.setdefault("bottom", OutfitPieceV2())
    selected.setdefault("one_piece", OutfitPieceV2())
    selected.setdefault("material", "weather-appropriate fabric")
    selected.setdefault("fit", "coherent comfortable fit")
    selected.setdefault("layering", "single coherent layering")
    request_lower = str(request_text or "").lower()
    selected["forbidden_details"] = (
        ()
        if mode != "free_outfit"
        else tuple(term for term in canonical_forbidden if term.lower() not in request_lower)
    )
    outfit = OutfitSpecV2(
        mode=mode,
        category=normalized_category,
        source="explicit_request" if explicit else "ambient_catalog",
        fingerprint=hashlib.sha256(repr((normalized_category, thermal_level, selected)).encode("utf-8", "ignore")).hexdigest(),
        **selected,
    )
    outfit.validate()
    return outfit
