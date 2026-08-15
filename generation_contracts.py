# -*- coding: utf-8 -*-
"""Versioned, backend-neutral contracts for image generation.

This module deliberately has no AstrBot or backend imports.  Companion facts,
model compilers and executors communicate through these immutable values so a
fallback can be recompiled without leaking prompt syntax across models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1


class ContractValidationError(ValueError):
    """Raised when a versioned generation contract is invalid."""


def _text(value: Any, limit: int = 1000) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _tuple_text(values: Iterable[Any] | None, limit: int = 300) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(value, limit) for value in (values or ()) if _text(value, limit)))


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"expected numeric value, got {value!r}") from exc


def thermal_level_for_temperature(value: float | None) -> str:
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
class WeatherFactsV1:
    temperature_c: float | None = None
    feels_like_c: float | None = None
    humidity_percent: float | None = None
    precipitation: str = ""
    wind: str = ""
    condition: str = ""
    observed_at: str = ""
    source: str = ""

    @property
    def effective_temperature_c(self) -> float | None:
        return self.feels_like_c if self.feels_like_c is not None else self.temperature_c

    @property
    def thermal_level(self) -> str:
        return thermal_level_for_temperature(self.effective_temperature_c)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WeatherFactsV1":
        value = value or {}
        return cls(
            temperature_c=_optional_float(value.get("temperature_c")),
            feels_like_c=_optional_float(value.get("feels_like_c")),
            humidity_percent=_optional_float(value.get("humidity_percent")),
            precipitation=_text(value.get("precipitation"), 120),
            wind=_text(value.get("wind"), 120),
            condition=_text(value.get("condition"), 120),
            observed_at=_text(value.get("observed_at"), 80),
            source=_text(value.get("source"), 80),
        )


@dataclass(frozen=True, slots=True)
class SceneContextV1:
    schema_version: int = SCHEMA_VERSION
    captured_at: str = ""
    timezone: str = "Asia/Shanghai"
    local_time: str = ""
    time_phase: str = "unknown"
    location_text: str = ""
    location_type: str = "unknown"
    indoor: bool | None = None
    current_activity: str = ""
    next_activity: str = ""
    sleep_phase: str = "awake"
    weather: WeatherFactsV1 = field(default_factory=WeatherFactsV1)
    valid_until: str = ""
    source: str = "companion"

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError(f"unsupported scene schema version: {self.schema_version}")
        if self.captured_at:
            try:
                datetime.fromisoformat(self.captured_at)
            except ValueError as exc:
                raise ContractValidationError("captured_at must be ISO-8601") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "SceneContextV1":
        value = value or {}
        scene = cls(
            schema_version=int(value.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
            captured_at=_text(value.get("captured_at"), 80),
            timezone=_text(value.get("timezone"), 80) or "Asia/Shanghai",
            local_time=_text(value.get("local_time"), 20),
            time_phase=_text(value.get("time_phase"), 40) or "unknown",
            location_text=_text(value.get("location_text"), 240),
            location_type=_text(value.get("location_type"), 60) or "unknown",
            indoor=value.get("indoor") if isinstance(value.get("indoor"), bool) else None,
            current_activity=_text(value.get("current_activity"), 300),
            next_activity=_text(value.get("next_activity"), 300),
            sleep_phase=_text(value.get("sleep_phase"), 60) or "awake",
            weather=WeatherFactsV1.from_mapping(value.get("weather") if isinstance(value.get("weather"), Mapping) else {}),
            valid_until=_text(value.get("valid_until"), 80),
            source=_text(value.get("source"), 80) or "companion",
        )
        scene.validate()
        return scene

    @classmethod
    def from_companion_snapshot(cls, value: Mapping[str, Any] | None) -> "SceneContextV1":
        value = value or {}
        schedule = value.get("schedule") if isinstance(value.get("schedule"), Mapping) else {}
        location = value.get("location") if isinstance(value.get("location"), Mapping) else {}
        weather = value.get("weather") if isinstance(value.get("weather"), Mapping) else {}
        sleep = value.get("sleep") if isinstance(value.get("sleep"), Mapping) else {}
        indoor: bool | None = None
        location_type = _text(location.get("category"), 60) or "unknown"
        if location_type == "home":
            indoor = True
        elif location_type == "outdoor":
            indoor = False
        return cls.from_mapping(
            {
                "captured_at": value.get("captured_at"),
                "local_time": value.get("time"),
                "time_phase": value.get("daypart"),
                "location_text": location.get("text"),
                "location_type": location_type,
                "indoor": indoor,
                "current_activity": schedule.get("activity") or schedule.get("text"),
                "sleep_phase": sleep.get("phase") or "awake",
                "weather": {
                    "temperature_c": weather.get("temperature_c"),
                    "feels_like_c": weather.get("feels_like_c"),
                    "condition": weather.get("text"),
                    "source": weather.get("source"),
                },
                "source": "companion_snapshot_v3",
            }
        )


@dataclass(frozen=True, slots=True)
class CharacterIdentitySpecV1:
    character_id: str = "bot"
    name: str = ""
    appearance: tuple[str, ...] = ()
    fixed_accessories: tuple[str, ...] = ()
    forbidden_features: tuple[str, ...] = ()
    default_style: str = ""
    clothing_policy: str = "scene_controlled"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CharacterIdentitySpecV1":
        value = value or {}
        return cls(
            character_id=_text(value.get("character_id"), 120) or "bot",
            name=_text(value.get("name"), 120),
            appearance=_tuple_text(value.get("appearance")),
            fixed_accessories=_tuple_text(value.get("fixed_accessories")),
            forbidden_features=_tuple_text(value.get("forbidden_features")),
            default_style=_text(value.get("default_style"), 160),
            clothing_policy=_text(value.get("clothing_policy"), 60) or "scene_controlled",
        )


REFERENCE_ROLES = frozenset(
    {
        "identity", "outfit", "pose", "style", "background", "control",
        "edit_source", "mask", "relationship_role", "continuity",
    }
)


@dataclass(frozen=True, slots=True)
class ReferenceBindingV1:
    reference_id: str
    path: str
    roles: tuple[str, ...] = ("identity",)
    subject: str = "bot"
    priority: int = 0
    preserve_clothing: bool = False
    strength: float | None = None
    source: str = "catalog"

    def validate(self) -> None:
        if not self.reference_id or not self.path:
            raise ContractValidationError("reference_id and path are required")
        invalid = set(self.roles) - REFERENCE_ROLES
        if invalid:
            raise ContractValidationError(f"unsupported reference roles: {sorted(invalid)}")
        if self.strength is not None and not 0 <= self.strength <= 2:
            raise ContractValidationError("reference strength must be between 0 and 2")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReferenceBindingV1":
        roles = _tuple_text(value.get("roles") or ("identity",), 60)
        item = cls(
            reference_id=_text(value.get("reference_id") or value.get("id"), 160),
            path=str(value.get("path") or "").strip(),
            roles=roles or ("identity",),
            subject=_text(value.get("subject"), 120) or "bot",
            priority=int(value.get("priority", 0) or 0),
            preserve_clothing=bool(value.get("preserve_clothing", False)),
            strength=_optional_float(value.get("strength")),
            source=_text(value.get("source"), 80) or "catalog",
        )
        item.validate()
        return item


@dataclass(frozen=True, slots=True)
class WardrobeSpecV1:
    category: str = "daily_outfit"
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    thermal_level: str = "unknown"
    source: str = "policy"
    user_override: bool = False
    lock_outfit: bool = False


@dataclass(frozen=True, slots=True)
class CompositionSpecV1:
    shot: str = ""
    location: str = ""
    lighting: str = ""
    subject_count: int = 1
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationSpecV1:
    schema_version: int
    request_id: str
    operation: str
    user_request: str
    scene: SceneContextV1
    character: CharacterIdentitySpecV1
    wardrobe: WardrobeSpecV1
    references: tuple[ReferenceBindingV1, ...] = ()
    composition: CompositionSpecV1 = field(default_factory=CompositionSpecV1)
    required_concepts: tuple[str, ...] = ()
    forbidden_concepts: tuple[str, ...] = ()
    legacy_prompt: str = ""

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError(f"unsupported generation schema version: {self.schema_version}")
        if not self.request_id or not self.operation:
            raise ContractValidationError("request_id and operation are required")
        if self.operation not in {"text2img", "selfie", "portrait", "edit"}:
            raise ContractValidationError(f"unsupported operation: {self.operation}")
        self.scene.validate()
        for reference in self.references:
            reference.validate()


@dataclass(frozen=True, slots=True)
class PromptPackageV1:
    schema_version: int = SCHEMA_VERSION
    model_profile: str = "legacy"
    positive_prompt: str = ""
    negative_prompt: str = ""
    auxiliary_prompts: Mapping[str, str] = field(default_factory=dict)
    required_concepts: tuple[str, ...] = ()
    forbidden_concepts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    compiler_version: str = "1"

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported prompt package version")
        if not self.model_profile or not self.positive_prompt:
            raise ContractValidationError("model_profile and positive_prompt are required")


@dataclass(frozen=True, slots=True)
class BackendCapabilitiesV1:
    schema_version: int = SCHEMA_VERSION
    text2img: bool = True
    edit: bool = False
    negative_prompt: bool = False
    max_reference_images: int = 0
    reference_roles: tuple[str, ...] = ()
    mask: bool = False
    seed: bool = False
    async_result: bool = True
    sizes: tuple[str, ...] = ()
    source: str = "conservative_default"

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported backend capabilities version")
        if self.max_reference_images < 0:
            raise ContractValidationError("max_reference_images must not be negative")


@dataclass(frozen=True, slots=True)
class WorkflowSlotV1:
    role: str
    node_id: str
    input_name: str
    value_type: str
    required: bool = False
    resolver: str = "manifest"
    confidence: str = "confirmed"


@dataclass(frozen=True, slots=True)
class WorkflowManifestV1:
    schema_version: int
    workflow_name: str
    fingerprint: str
    model_profiles: tuple[str, ...]
    operations: tuple[str, ...]
    slots: tuple[WorkflowSlotV1, ...]
    mapping_version: int = 1
    source: str = "manifest"

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported workflow manifest version")
        if not self.workflow_name or not self.fingerprint:
            raise ContractValidationError("workflow_name and fingerprint are required")
        seen: set[tuple[str, str]] = set()
        for slot in self.slots:
            key = (slot.node_id, slot.input_name)
            if key in seen:
                raise ContractValidationError(f"duplicate workflow slot target: {key}")
            seen.add(key)


@dataclass(frozen=True, slots=True)
class GenerationResultV1:
    schema_version: int = SCHEMA_VERSION
    request_id: str = ""
    task_id: str = ""
    backend: str = ""
    model_profile: str = ""
    workflow: str = ""
    image_path: str = ""
    submitted_reference_ids: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()
    generation_completed: bool = False
    failure_stage: str = ""
    error_code: str = ""
    note: str = ""
    trace: tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.image_path and not self.error_code)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported generation result version")
        if self.error_code and self.image_path:
            raise ContractValidationError("failed generation result must not expose image_path")


def contract_dict(value: Any) -> dict[str, Any]:
    """Return a JSON-compatible mapping for a contract dataclass."""
    return asdict(value)


__all__ = [name for name in globals() if name.endswith("V1")] + [
    "SCHEMA_VERSION", "ContractValidationError", "contract_dict",
    "thermal_level_for_temperature", "REFERENCE_ROLES",
]
