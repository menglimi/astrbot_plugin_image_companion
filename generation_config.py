# -*- coding: utf-8 -*-
"""Configuration parsing, migration preview and route diagnostics."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

try:
    from .generation_adapters import endpoint_capabilities, endpoint_model_profile, redact_sensitive
    from .generation_engine import RouteDefinition, RouteKey, RouteRegistry
    from .generation_profiles import default_model_profile_registry
except ImportError:  # pragma: no cover
    from generation_adapters import endpoint_capabilities, endpoint_model_profile, redact_sensitive
    from generation_engine import RouteDefinition, RouteKey, RouteRegistry
    from generation_profiles import default_model_profile_registry


ENGINE_MODES = frozenset({"legacy", "shadow", "active"})


@dataclass(frozen=True, slots=True)
class RolloutConfig:
    mode: str = "legacy"
    instant_rollback: bool = False
    anima_scopes: tuple[str, ...] = ("selfie",)
    output_validation: bool = False
    allowed_reference_roots: tuple[str, ...] = ()

    @property
    def engine_enabled(self) -> bool:
        return self.mode in {"shadow", "active"} and not self.instant_rollback

    @property
    def shadow_mode(self) -> bool:
        return self.mode == "shadow" and not self.instant_rollback


@dataclass(frozen=True, slots=True)
class ConfigValidation:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    routes: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def parse_rollout_config(config: Mapping[str, Any]) -> RolloutConfig:
    engine = _mapping(config.get("engine"))
    mode = str(engine.get("mode") or "legacy").strip().lower()
    if mode not in ENGINE_MODES:
        mode = "legacy"
    scopes = tuple(
        dict.fromkeys(
            str(item).strip().lower()
            for item in _list(engine.get("anima_scopes") or ["selfie"])
            if str(item).strip()
        )
    )
    roots = tuple(str(item).strip() for item in _list(engine.get("allowed_reference_roots")) if str(item).strip())
    return RolloutConfig(
        mode=mode,
        instant_rollback=bool(engine.get("instant_rollback", False)),
        anima_scopes=scopes,
        output_validation=bool(engine.get("output_validation", False)),
        allowed_reference_roots=roots,
    )


def build_route_registry(config: Mapping[str, Any]) -> tuple[RouteRegistry, ConfigValidation]:
    engine = _mapping(config.get("engine"))
    rows = _list(engine.get("routes"))
    mapping_rows = _list(engine.get("workflow_mappings"))
    workflow_mappings: dict[str, Mapping[str, Any]] = {}
    for raw_mapping in mapping_rows:
        if not isinstance(raw_mapping, Mapping):
            continue
        mapping_key = str(
            raw_mapping.get("route")
            or raw_mapping.get("workflow")
            or raw_mapping.get("workflow_id")
            or ""
        ).strip()
        slots = raw_mapping.get("mapping") or raw_mapping.get("slots")
        if mapping_key and isinstance(slots, Mapping):
            workflow_mappings[mapping_key] = raw_mapping
    registry = RouteRegistry()
    errors: list[str] = []
    warnings: list[str] = []
    serialized: list[dict[str, Any]] = []
    profiles = set(default_model_profile_registry().names())
    names: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            errors.append(f"route[{index}] must be an object")
            continue
        name = str(raw.get("name") or "").strip()
        backend = str(raw.get("backend") or "").strip().lower()
        profile = str(raw.get("model_profile") or "legacy").strip().lower()
        operation = str(raw.get("operation") or "selfie").strip().lower()
        workflow = str(raw.get("workflow") or "").strip()
        if not name or name in names:
            errors.append(f"route[{index}] has a missing or duplicate name")
            continue
        names.add(name)
        if backend not in {"comfyui", "external", "legacy"}:
            errors.append(f"route {name!r} has unsupported backend {backend!r}")
        if profile not in profiles:
            errors.append(f"route {name!r} has unknown model profile {profile!r}")
        if operation not in {"text2img", "selfie", "portrait", "edit"}:
            errors.append(f"route {name!r} has unsupported operation {operation!r}")
        if backend in {"comfyui", "external"} and not workflow:
            errors.append(f"route {name!r} requires workflow/endpoint id")
        paid = bool(raw.get("paid", backend == "external"))
        allow_paid = bool(raw.get("allow_paid_fallback", False))
        fallbacks = tuple(str(item).strip() for item in _list(raw.get("fallback_routes")) if str(item).strip())
        mapping_row = workflow_mappings.get(name) or workflow_mappings.get(workflow) or {}
        mapping = mapping_row.get("mapping") or mapping_row.get("slots")
        route = RouteDefinition(
            name=name,
            key=RouteKey(backend, profile, operation, workflow),
            enabled=bool(raw.get("enabled", True)),
            fallback_routes=fallbacks,
            allow_paid_fallback=allow_paid,
            paid=paid,
            timeout_seconds=max(5, min(900, int(raw.get("timeout_seconds", 180) or 180))),
            concurrency_limit=max(1, min(16, int(raw.get("concurrency_limit", 1) or 1))),
            settings={
                "scope": str(raw.get("scope") or ""),
                "mapping": dict(mapping) if isinstance(mapping, Mapping) else {},
                "workflow_fingerprint": str(
                    mapping_row.get("fingerprint")
                    or raw.get("workflow_fingerprint")
                    or ""
                ).strip(),
                "mapping_version": int(
                    mapping_row.get("mapping_version")
                    or raw.get("mapping_version")
                    or 0
                ),
            },
        )
        try:
            registry.register(route)
        except ValueError as exc:
            errors.append(str(exc))
        serialized.append({
            "name": name,
            "backend": backend,
            "model_profile": profile,
            "operation": operation,
            "workflow": workflow,
            "enabled": route.enabled,
            "paid": paid,
            "fallback_routes": list(fallbacks),
        })
    for route in registry.list():
        for fallback in route.fallback_routes:
            if fallback not in names:
                errors.append(f"route {route.name!r} refers to missing fallback {fallback!r}")
            elif registry.get(fallback).paid and not route.allow_paid_fallback:
                warnings.append(f"route {route.name!r} paid fallback {fallback!r} will be skipped")
    for route in registry.list():
        seen: set[str] = set()
        pending = list(route.fallback_routes)
        while pending:
            target = pending.pop()
            if target == route.name:
                errors.append(f"route {route.name!r} has a fallback cycle")
                break
            if target in seen or target not in names:
                continue
            seen.add(target)
            pending.extend(registry.get(target).fallback_routes)
    return registry, ConfigValidation(not errors, tuple(errors), tuple(warnings), tuple(serialized))


def legacy_migration_preview(config: Mapping[str, Any]) -> dict[str, Any]:
    image = _mapping(config.get("image"))
    backend = str(image.get("photo_generation_backend") or "auto").lower()
    routes: list[dict[str, Any]] = []
    for operation, key in (("text2img", "comfyui_text2img_workflow_name"), ("selfie", "comfyui_selfie_workflow_name")):
        workflow = str(image.get(key) or "").strip()
        if workflow:
            routes.append({
                "name": f"legacy-comfyui-{operation}", "backend": "comfyui", "model_profile": "legacy",
                "operation": operation, "workflow": workflow, "enabled": backend in {"auto", "comfyui"},
            })
    endpoints = _list(image.get("external_image_api_endpoints"))
    if not endpoints and image.get("external_image_api_base_url"):
        endpoints = [{
            "name": "legacy-primary", "platform": image.get("external_image_api_platform"),
            "base_url": image.get("external_image_api_base_url"), "model": image.get("external_image_api_model"),
        }]
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, Mapping):
            continue
        endpoint_id = str(endpoint.get("name") or endpoint.get("id") or f"endpoint-{index + 1}")
        routes.append({
            "name": f"legacy-external-{index + 1}", "backend": "external",
            "model_profile": endpoint_model_profile(endpoint), "operation": "text2img", "workflow": endpoint_id,
            "paid": True, "capabilities": asdict(endpoint_capabilities(endpoint)),
        })
    return {
        "source_backend": backend,
        "routes": redact_sensitive(routes),
        "changes_applied": False,
        "note": "preview only; legacy configuration remains authoritative until active mode is enabled",
    }


def route_diagnostics(config: Mapping[str, Any]) -> dict[str, Any]:
    rollout = parse_rollout_config(config)
    registry, validation = build_route_registry(config)
    image = _mapping(config.get("image"))
    endpoints = _list(image.get("external_image_api_endpoints"))
    endpoint_rows: list[dict[str, Any]] = []
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, Mapping):
            continue
        endpoint_rows.append({
            "id": str(endpoint.get("name") or endpoint.get("id") or f"endpoint-{index + 1}"),
            "platform": str(endpoint.get("platform") or "auto"),
            "model": str(endpoint.get("model") or ""),
            "model_profile": endpoint_model_profile(endpoint),
            "capabilities": asdict(endpoint_capabilities(endpoint)),
        })
    return {
        "rollout": asdict(rollout),
        "effective_engine_enabled": rollout.engine_enabled,
        "effective_shadow_mode": rollout.shadow_mode,
        "validation": validation.as_dict(),
        "route_keys": [route.key.value() for route in registry.list()],
        "migration_preview": legacy_migration_preview(config),
        "online_endpoints": redact_sensitive(endpoint_rows),
        "available_model_profiles": list(default_model_profile_registry().names()),
    }


def active_engine_claims_profile(
    config: Mapping[str, Any],
    model_profile: str,
    *,
    operation: str = "",
) -> bool:
    """Return whether an active, valid unified route owns a model profile.

    Shadow routes deliberately return false so they cannot suppress an
    officially selected legacy or direct backend.
    """
    rollout = parse_rollout_config(config)
    if rollout.mode != "active" or not rollout.engine_enabled:
        return False
    registry, validation = build_route_registry(config)
    if not validation.ok:
        return False
    profile = str(model_profile or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    if not profile:
        return False
    return any(
        route.enabled
        and route.key.model_profile == profile
        and (not normalized_operation or route.key.operation == normalized_operation)
        for route in registry.list()
    )


__all__ = [
    "ENGINE_MODES", "RolloutConfig", "ConfigValidation", "parse_rollout_config",
    "build_route_registry", "legacy_migration_preview", "route_diagnostics",
    "active_engine_claims_profile",
]
