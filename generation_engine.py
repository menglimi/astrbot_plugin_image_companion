# -*- coding: utf-8 -*-
"""Backend-isolated generation router with recompiling fallback semantics."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Protocol

try:
    from .generation_contracts import (
        BackendCapabilitiesV1,
        GenerationResultV1,
        GenerationSpecV1,
        PromptPackageV1,
        ReferenceBindingV1,
    )
    from .generation_profiles import ModelProfileRegistry
except ImportError:  # pragma: no cover
    from generation_contracts import (
        BackendCapabilitiesV1,
        GenerationResultV1,
        GenerationSpecV1,
        PromptPackageV1,
        ReferenceBindingV1,
    )
    from generation_profiles import ModelProfileRegistry


ERROR_CODES = frozenset(
    {
        "context_invalid", "route_unavailable", "compiler_failed", "workflow_ambiguous",
        "mapping_invalid", "capability_missing", "submission_failed", "backend_timeout",
        "result_materialization_failed", "output_validation_failed", "fallback_not_allowed",
    }
)


@dataclass(frozen=True, slots=True)
class RouteKey:
    backend: str
    model_profile: str
    operation: str
    workflow: str = ""

    def value(self) -> str:
        return "/".join((self.backend, self.model_profile, self.operation, self.workflow or "default"))


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    name: str
    key: RouteKey
    enabled: bool = True
    fallback_routes: tuple[str, ...] = ()
    allow_paid_fallback: bool = False
    paid: bool = False
    timeout_seconds: int = 180
    concurrency_limit: int = 1
    capabilities_override: BackendCapabilitiesV1 | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


class RouteRegistry:
    def __init__(self) -> None:
        self._routes: dict[str, RouteDefinition] = {}

    def register(self, route: RouteDefinition) -> None:
        if not route.name:
            raise ValueError("route name is required")
        if route.name in self._routes:
            raise ValueError(f"duplicate route: {route.name}")
        self._routes[route.name] = route

    def get(self, name: str) -> RouteDefinition:
        if name not in self._routes:
            raise KeyError(f"unknown route: {name}")
        return self._routes[name]

    def list(self) -> tuple[RouteDefinition, ...]:
        return tuple(self._routes.values())


@dataclass(frozen=True, slots=True)
class ReferencePlan:
    submitted: tuple[ReferenceBindingV1, ...]
    omitted: tuple[ReferenceBindingV1, ...]
    degraded_capabilities: tuple[str, ...]
    allowed: bool
    reason: str = ""


class ReferencePlanner:
    def plan(
        self,
        references: tuple[ReferenceBindingV1, ...],
        capabilities: BackendCapabilitiesV1,
        *,
        required_roles: tuple[str, ...] = (),
    ) -> ReferencePlan:
        supported_roles = set(capabilities.reference_roles)
        ordered = sorted(references, key=lambda item: (-item.priority, item.reference_id))
        compatible = [
            item for item in ordered
            if not supported_roles or bool(set(item.roles) & supported_roles)
        ]
        capacity = max(0, int(capabilities.max_reference_images))
        submitted = tuple(compatible[:capacity])
        omitted = tuple(item for item in references if item not in submitted)
        submitted_roles = {role for item in submitted for role in item.roles}
        missing_required = set(required_roles) - submitted_roles
        degraded: list[str] = []
        if omitted:
            degraded.append(f"references:{len(submitted)}/{len(references)}")
        if missing_required:
            degraded.append("missing_roles:" + ",".join(sorted(missing_required)))
        allowed = not missing_required
        return ReferencePlan(
            submitted=submitted,
            omitted=omitted,
            degraded_capabilities=tuple(degraded),
            allowed=allowed,
            reason=("required reference roles unavailable" if missing_required else ""),
        )


class BackendAdapter(Protocol):
    backend: str

    async def capabilities(self, route: RouteDefinition) -> BackendCapabilitiesV1: ...

    async def generate(
        self,
        route: RouteDefinition,
        spec: GenerationSpecV1,
        prompt: PromptPackageV1,
        references: ReferencePlan,
        trace: list[dict[str, Any]],
    ) -> GenerationResultV1: ...


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 60) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1, cooldown_seconds)
        self._state: dict[str, tuple[int, float]] = {}

    def available(self, route_name: str, now: float | None = None) -> bool:
        failures, opened_at = self._state.get(route_name, (0, 0.0))
        if failures < self.failure_threshold:
            return True
        return (now if now is not None else time.monotonic()) - opened_at >= self.cooldown_seconds

    def success(self, route_name: str) -> None:
        self._state.pop(route_name, None)

    def failure(self, route_name: str, now: float | None = None) -> None:
        failures, opened_at = self._state.get(route_name, (0, 0.0))
        failures += 1
        self._state[route_name] = (
            failures,
            (now if now is not None else time.monotonic()) if failures >= self.failure_threshold else opened_at,
        )


def route_cache_key(
    spec: GenerationSpecV1,
    route: RouteDefinition,
    *,
    workflow_fingerprint: str = "",
    mapping_version: int = 0,
) -> str:
    semantic_spec = {
        "operation": spec.operation,
        "user_request": spec.user_request,
        "scene": asdict(spec.scene),
        "character": asdict(spec.character),
        "wardrobe": asdict(spec.wardrobe),
        "composition": asdict(spec.composition),
        "required_concepts": spec.required_concepts,
        "forbidden_concepts": spec.forbidden_concepts,
        "legacy_prompt": spec.legacy_prompt,
        "route": route.key.value(),
        "workflow_fingerprint": workflow_fingerprint,
        "mapping_version": mapping_version,
    }
    raw = json.dumps(semantic_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


class GenerationEngine:
    def __init__(
        self,
        profiles: ModelProfileRegistry,
        routes: RouteRegistry,
        adapters: Mapping[str, BackendAdapter],
        *,
        reference_planner: ReferencePlanner | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        enabled: bool = True,
        shadow_mode: bool = False,
        output_validator: Callable[[str], tuple[bool, str]] | None = None,
        metrics: Any = None,
        prompt_cache: dict[str, PromptPackageV1] | None = None,
        prompt_cache_limit: int = 256,
        route_semaphores: dict[str, asyncio.Semaphore] | None = None,
    ) -> None:
        self.profiles = profiles
        self.routes = routes
        self.adapters = dict(adapters)
        self.reference_planner = reference_planner or ReferencePlanner()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.enabled = enabled
        self.shadow_mode = shadow_mode
        self.output_validator = output_validator
        self.metrics = metrics
        self._prompt_cache = prompt_cache if prompt_cache is not None else {}
        self._prompt_cache_limit = max(1, int(prompt_cache_limit))
        self._route_semaphores = route_semaphores if route_semaphores is not None else {}

    @staticmethod
    def _event(stage: str, **data: Any) -> dict[str, Any]:
        return {"stage": stage, "at": time.time(), "data": data}

    async def generate(self, spec: GenerationSpecV1, route_name: str) -> GenerationResultV1:
        started = time.monotonic()
        trace: list[dict[str, Any]] = [self._event("context", request_id=spec.request_id)]
        try:
            spec.validate()
        except Exception as exc:
            result = GenerationResultV1(
                request_id=spec.request_id,
                error_code="context_invalid",
                failure_stage="context",
                note=f"{type(exc).__name__}: {exc}",
                trace=tuple(trace),
            )
            self._record_metrics(route_name, result, started)
            return result
        if not self.enabled:
            result = GenerationResultV1(
                request_id=spec.request_id,
                error_code="route_unavailable",
                failure_stage="route",
                note="generation engine disabled",
                trace=tuple(trace),
            )
            self._record_metrics(route_name, result, started)
            return result
        result = await self._attempt(spec, route_name, trace, visited=())
        self._record_metrics(route_name, result, started)
        return result

    def _record_metrics(self, route_name: str, result: GenerationResultV1, started: float) -> None:
        if self.metrics is not None and callable(getattr(self.metrics, "record", None)):
            self.metrics.record(
                route=route_name,
                ok=result.ok,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_code=result.error_code,
            )

    def _cache_prompt(self, key: str, prompt: PromptPackageV1) -> None:
        if key in self._prompt_cache:
            self._prompt_cache.pop(key, None)
        while len(self._prompt_cache) >= self._prompt_cache_limit:
            oldest = next(iter(self._prompt_cache), None)
            if oldest is None:
                break
            self._prompt_cache.pop(oldest, None)
        self._prompt_cache[key] = prompt

    def _route_semaphore(self, route: RouteDefinition) -> asyncio.Semaphore:
        key = f"{route.name}:{max(1, int(route.concurrency_limit))}"
        semaphore = self._route_semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(1, int(route.concurrency_limit)))
            self._route_semaphores[key] = semaphore
        return semaphore

    async def _attempt(
        self,
        spec: GenerationSpecV1,
        route_name: str,
        trace: list[dict[str, Any]],
        *,
        visited: tuple[str, ...],
    ) -> GenerationResultV1:
        if route_name in visited:
            return GenerationResultV1(
                request_id=spec.request_id,
                error_code="route_unavailable",
                failure_stage="route",
                note="fallback cycle detected",
                trace=tuple(trace),
            )
        try:
            route = self.routes.get(route_name)
        except KeyError as exc:
            return GenerationResultV1(
                request_id=spec.request_id,
                error_code="route_unavailable",
                failure_stage="route",
                note=str(exc),
                trace=tuple(trace),
            )
        trace.append(self._event("route", route=route.name, key=route.key.value()))
        adapter = self.adapters.get(route.key.backend)
        if not route.enabled or adapter is None or not self.circuit_breaker.available(route.name):
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                error_code="route_unavailable",
                failure_stage="route",
                note="route disabled, adapter missing, or circuit open",
                trace=tuple(trace),
            )
            return await self._fallback(spec, route, result, trace, (*visited, route_name))
        try:
            cache_key = route_cache_key(
                spec,
                route,
                workflow_fingerprint=str(route.settings.get("workflow_fingerprint") or ""),
                mapping_version=int(route.settings.get("mapping_version", 0) or 0),
            )
            prompt = self._prompt_cache.get(cache_key)
            if prompt is None:
                compiler = self.profiles.get(route.key.model_profile)
                prompt = compiler.compile(spec)
                prompt.validate()
                self._cache_prompt(cache_key, prompt)
        except Exception as exc:
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                error_code="compiler_failed",
                failure_stage="compiler",
                note=f"{type(exc).__name__}: {exc}",
                trace=tuple(trace),
            )
            return await self._fallback(spec, route, result, trace, (*visited, route_name))
        trace.append(
            self._event(
                "compiler",
                profile=prompt.model_profile,
                positive_hash=hashlib.sha256(prompt.positive_prompt.encode()).hexdigest(),
                negative_hash=hashlib.sha256(prompt.negative_prompt.encode()).hexdigest(),
            )
        )
        try:
            capabilities = route.capabilities_override or await adapter.capabilities(route)
            capabilities.validate()
            required_roles = tuple(
                dict.fromkeys(role for reference in spec.references for role in reference.roles if role == "edit_source")
            )
            reference_plan = self.reference_planner.plan(
                spec.references, capabilities, required_roles=required_roles
            )
        except Exception as exc:
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                error_code="capability_missing",
                failure_stage="capability",
                note=f"{type(exc).__name__}: {exc}",
                trace=tuple(trace),
            )
            return await self._fallback(spec, route, result, trace, (*visited, route_name))
        trace.append(
            self._event(
                "reference_plan",
                submitted=[item.reference_id for item in reference_plan.submitted],
                omitted=[item.reference_id for item in reference_plan.omitted],
                degraded=list(reference_plan.degraded_capabilities),
            )
        )
        if not reference_plan.allowed:
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                degraded_capabilities=reference_plan.degraded_capabilities,
                error_code="capability_missing",
                failure_stage="reference_plan",
                note=reference_plan.reason,
                trace=tuple(trace),
            )
            return await self._fallback(spec, route, result, trace, (*visited, route_name))
        if self.shadow_mode:
            return GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                submitted_reference_ids=tuple(item.reference_id for item in reference_plan.submitted),
                degraded_capabilities=reference_plan.degraded_capabilities,
                note="shadow_mode: submission skipped",
                trace=tuple((*trace, self._event("shadow", route=route.name))),
            )
        try:
            async with self._route_semaphore(route):
                result = await adapter.generate(route, spec, prompt, reference_plan, trace)
        except TimeoutError as exc:
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                error_code="backend_timeout",
                failure_stage="submission",
                note=str(exc) or "backend timeout",
                trace=tuple(trace),
            )
        except Exception as exc:
            result = GenerationResultV1(
                request_id=spec.request_id,
                backend=route.key.backend,
                model_profile=route.key.model_profile,
                workflow=route.key.workflow,
                error_code="submission_failed",
                failure_stage="submission",
                note=f"{type(exc).__name__}: {exc}",
                trace=tuple(trace),
            )
        if result.ok and self.output_validator is not None:
            valid, note = self.output_validator(result.image_path)
            if not valid:
                result = replace(
                    result,
                    image_path="",
                    error_code="output_validation_failed",
                    failure_stage="output_validation",
                    note=note,
                )
        if result.ok:
            self.circuit_breaker.success(route.name)
            return result
        self.circuit_breaker.failure(route.name)
        return await self._fallback(spec, route, result, trace, (*visited, route_name))

    async def _fallback(
        self,
        spec: GenerationSpecV1,
        route: RouteDefinition,
        result: GenerationResultV1,
        trace: list[dict[str, Any]],
        visited: tuple[str, ...],
    ) -> GenerationResultV1:
        for fallback_name in route.fallback_routes:
            try:
                fallback = self.routes.get(fallback_name)
            except KeyError:
                continue
            if fallback.paid and not route.allow_paid_fallback:
                trace.append(self._event("fallback_skipped", route=fallback_name, reason="paid"))
                continue
            trace.append(self._event("fallback", source=route.name, target=fallback_name))
            candidate = await self._attempt(spec, fallback_name, trace, visited=visited)
            if candidate.ok:
                return candidate
            result = candidate
        return replace(result, trace=tuple(trace))


__all__ = [
    "ERROR_CODES", "RouteKey", "RouteDefinition", "RouteRegistry", "ReferencePlan",
    "ReferencePlanner", "BackendAdapter", "CircuitBreaker", "route_cache_key",
    "GenerationEngine",
]
