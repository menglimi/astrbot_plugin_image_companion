# -*- coding: utf-8 -*-
"""Backend adapters, endpoint capability profiles and rollout controls."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

try:
    from .generation_contracts import BackendCapabilitiesV1, GenerationResultV1
    from .generation_engine import ReferencePlan, RouteDefinition
except ImportError:  # pragma: no cover
    from generation_contracts import BackendCapabilitiesV1, GenerationResultV1
    from generation_engine import ReferencePlan, RouteDefinition


def redact_sensitive(value: Any) -> Any:
    """Redact credential-like fields and URL query secrets recursively."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "***"
                if re.search(r"(?:api[_-]?key|token|secret|authorization|password)", str(key), re.I)
                else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return re.sub(r"([?&](?:key|token|access_token|api_key)=)[^&]+", r"\1***", value, flags=re.I)
    return value


def path_within_roots(path: str, roots: tuple[str, ...]) -> bool:
    try:
        candidate = Path(path).resolve(strict=True)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            resolved_root = Path(root).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if candidate == resolved_root or resolved_root in candidate.parents:
            return True
    return False


def endpoint_capabilities(endpoint: Mapping[str, Any]) -> BackendCapabilitiesV1:
    explicit = endpoint.get("capabilities") if isinstance(endpoint.get("capabilities"), Mapping) else {}
    model = str(endpoint.get("model") or "").lower()
    platform = str(endpoint.get("platform") or "auto").lower()
    inferred_reference = any(token in model for token in ("gpt-image", "gemini", "edit", "kontext"))
    inferred_multi = bool(re.search(r"gpt[-_]?image[-_]?2", model))
    reference_count = int(explicit.get("max_reference_images", 4 if inferred_multi else (1 if inferred_reference else 0)) or 0)
    roles = explicit.get("reference_roles")
    if not isinstance(roles, (list, tuple)):
        roles = ("identity", "outfit", "edit_source") if reference_count else ()
    return BackendCapabilitiesV1(
        text2img=bool(explicit.get("text2img", True)),
        edit=bool(explicit.get("edit", inferred_reference)),
        negative_prompt=bool(explicit.get("negative_prompt", platform in {"novelai", "nai", "openai", "openrouter"})),
        max_reference_images=max(0, reference_count),
        reference_roles=tuple(str(item) for item in roles),
        mask=bool(explicit.get("mask", False)),
        seed=bool(explicit.get("seed", False)),
        async_result=bool(explicit.get("async_result", True)),
        sizes=tuple(str(item) for item in (explicit.get("sizes") or ())),
        source="endpoint_override" if explicit else "model_inference",
    )


def endpoint_model_profile(endpoint: Mapping[str, Any]) -> str:
    explicit = str(endpoint.get("model_profile") or "").strip().lower()
    if explicit:
        return explicit
    platform = str(endpoint.get("platform") or "").lower()
    model = str(endpoint.get("model") or "").lower()
    if platform in {"novelai", "nai"} or "nai" in model or "novelai" in model:
        return "nai"
    if bool(endpoint.get("tag_prompt")):
        return "generic_tags"
    return "generic_natural"


class ComfyUIService(Protocol):
    def inspect_workflow(self, workflow_id: str) -> Mapping[str, Any]: ...
    async def submit_generation(self, workflow_id: str, slots: Mapping[str, Any], *, mapping: Mapping[str, Any] | None = None) -> Mapping[str, Any]: ...
    async def get_status(self, task_id: str) -> Mapping[str, Any]: ...
    async def get_result(self, task_id: str) -> Mapping[str, Any]: ...
    async def cancel(self, task_id: str) -> Mapping[str, Any]: ...


class ComfyUIServiceAdapter:
    backend = "comfyui"

    def __init__(
        self,
        service: ComfyUIService,
        *,
        allowed_reference_roots: tuple[str, ...] = (),
        materialize: Callable[[str, str], Awaitable[str]] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.service = service
        self.allowed_reference_roots = allowed_reference_roots
        self.materialize = materialize
        self.poll_interval = max(0.01, poll_interval)

    async def capabilities(self, route: RouteDefinition) -> BackendCapabilitiesV1:
        inspection = self.service.inspect_workflow(route.key.workflow)
        slots = inspection.get("slots") if isinstance(inspection.get("slots"), list) else []
        names = {str(item.get("name")) for item in slots if isinstance(item, Mapping)}
        reference_names = {
            name for name in names
            if name.startswith("reference_image_") or name.endswith("_image")
        }
        roles = tuple(
            role for role, name in (
                ("identity", "identity_image"), ("outfit", "outfit_image"),
                ("background", "background_image"), ("style", "style_image"),
                ("control", "control_image"), ("edit_source", "reference_image_1"),
            ) if name in names
        )
        return BackendCapabilitiesV1(
            text2img="positive_prompt" in names,
            edit=bool(reference_names),
            negative_prompt="negative_prompt" in names,
            max_reference_images=len(reference_names),
            reference_roles=roles,
            mask="mask" in names,
            seed="seed" in names,
            async_result=True,
            source="workflow_inspection",
        )

    def _reference_value(self, path: str) -> str:
        if self.allowed_reference_roots and not path_within_roots(path, self.allowed_reference_roots):
            raise ValueError("reference path is outside the configured roots")
        data = Path(path).read_bytes()
        if not data:
            raise ValueError("reference image is empty")
        return base64.b64encode(data).decode("ascii")

    async def generate(self, route, spec, prompt, references: ReferencePlan, trace):
        inspection = self.service.inspect_workflow(route.key.workflow)
        inspection_slots = inspection.get("slots") if isinstance(inspection.get("slots"), list) else []
        available_slot_names = {
            str(item.get("name")) for item in inspection_slots if isinstance(item, Mapping)
        }
        expected_fingerprint = str(route.settings.get("workflow_fingerprint") or "").strip()
        actual_fingerprint = str(inspection.get("fingerprint") or "").strip()
        if expected_fingerprint and expected_fingerprint != actual_fingerprint:
            raise ValueError("workflow fingerprint changed; confirmed mapping must be reviewed")
        trace.append({
            "stage": "workflow_mapping",
            "at": time.time(),
            "data": {
                "workflow": route.key.workflow,
                "fingerprint": actual_fingerprint,
                "mapping_source": "confirmed" if route.settings.get("mapping") else "service_resolution",
            },
        })
        capabilities = await self.capabilities(route)
        slots: dict[str, Any] = {"positive_prompt": prompt.positive_prompt}
        if prompt.negative_prompt and capabilities.negative_prompt:
            slots["negative_prompt"] = prompt.negative_prompt
        outfit = getattr(spec.wardrobe, "outfit", None)
        outfit_mode = str(getattr(outfit, "mode", "") or "")
        mode_parameters = route.settings.get("outfit_mode_parameters")
        selected_parameters = (
            mode_parameters.get(outfit_mode)
            if isinstance(mode_parameters, Mapping) and isinstance(mode_parameters.get(outfit_mode), Mapping)
            else {}
        )
        allowed_parameters = {
            "lora_strength", "lora_clip_strength", "ipadapter_weight", "face_strength", "control_strength",
        }
        applied_parameters: dict[str, float] = {}
        for name, raw_value in selected_parameters.items():
            parameter = str(name or "").strip()
            if parameter not in allowed_parameters or parameter not in available_slot_names:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not 0 <= value <= 2:
                continue
            slots[parameter] = value
            applied_parameters[parameter] = value
        if selected_parameters:
            trace.append({
                "stage": "outfit_mode_parameters",
                "at": time.time(),
                "data": {
                    "mode": outfit_mode,
                    "requested": sorted(str(name) for name in selected_parameters),
                    "applied": applied_parameters,
                },
            })
        role_names = {
            "identity": "identity_image", "outfit": "outfit_image",
            "background": "background_image", "style": "style_image", "control": "control_image",
        }
        generic_index = 0
        for reference in references.submitted:
            role = next((item for item in reference.roles if item in role_names), "")
            name = role_names.get(role, "")
            if not name:
                generic_index += 1
                name = f"reference_image_{generic_index}"
            slots[name] = self._reference_value(reference.path)
        route_mapping = route.settings.get("mapping")
        submitted = await self.service.submit_generation(
            route.key.workflow,
            slots,
            mapping=route_mapping if isinstance(route_mapping, Mapping) and route_mapping else None,
        )
        task_id = str(submitted.get("task_id") or "")
        if not task_id:
            return GenerationResultV1(request_id=spec.request_id, backend=self.backend, error_code="submission_failed", failure_stage="submission")
        trace.append({
            "stage": "submission",
            "at": time.time(),
            "data": {"task_id": task_id, "slot_names": sorted(slots)},
        })
        deadline = time.monotonic() + max(1, route.timeout_seconds)
        while time.monotonic() < deadline:
            result = await self.service.get_result(task_id)
            if result.get("status") == "completed":
                outputs = result.get("outputs") if isinstance(result.get("outputs"), list) else []
                image = next((item for item in outputs if isinstance(item, Mapping) and item.get("kind") == "images"), None)
                if image:
                    url = str(image.get("url") or "")
                    path = await self.materialize(url, spec.request_id) if self.materialize and url else url
                    trace.append({
                        "stage": "result",
                        "at": time.time(),
                        "data": {"task_id": task_id, "materialized": bool(path)},
                    })
                    return GenerationResultV1(
                        request_id=spec.request_id, task_id=task_id, backend=self.backend,
                        model_profile=prompt.model_profile, workflow=route.key.workflow,
                        image_path=path, submitted_reference_ids=tuple(item.reference_id for item in references.submitted),
                        degraded_capabilities=references.degraded_capabilities, generation_completed=True,
                        trace=tuple(trace),
                    )
            await asyncio.sleep(self.poll_interval)
        await self.service.cancel(task_id)
        return GenerationResultV1(
            request_id=spec.request_id, task_id=task_id, backend=self.backend,
            model_profile=prompt.model_profile, workflow=route.key.workflow,
            error_code="backend_timeout", failure_stage="result", note="ComfyUI result timeout", trace=tuple(trace),
        )


class OnlineEndpointAdapter:
    backend = "external"

    def __init__(
        self,
        endpoints: Mapping[str, Mapping[str, Any]],
        execute: Callable[[Mapping[str, Any], Any, tuple[Any, ...]], Awaitable[Mapping[str, Any]]],
    ) -> None:
        self.endpoints = {str(key): dict(value) for key, value in endpoints.items()}
        self.execute = execute

    async def capabilities(self, route: RouteDefinition) -> BackendCapabilitiesV1:
        return endpoint_capabilities(self.endpoints.get(route.key.workflow, {}))

    async def generate(self, route, spec, prompt, references: ReferencePlan, trace):
        endpoint = self.endpoints.get(route.key.workflow)
        if endpoint is None:
            return GenerationResultV1(request_id=spec.request_id, backend=self.backend, error_code="route_unavailable", failure_stage="route")
        trace.append({
            "stage": "submission",
            "at": time.time(),
            "data": {
                "endpoint_id": route.key.workflow,
                "model_profile": prompt.model_profile,
                "reference_count": len(references.submitted),
            },
        })
        outcome = await self.execute(endpoint, prompt, references.submitted)
        trace.append({
            "stage": "result",
            "at": time.time(),
            "data": {
                "endpoint_id": route.key.workflow,
                "materialized": bool(outcome.get("image_path")),
                "failure_stage": str(outcome.get("failure_stage") or ""),
            },
        })
        return GenerationResultV1(
            request_id=spec.request_id,
            task_id=str(outcome.get("task_id") or ""),
            backend=self.backend,
            model_profile=prompt.model_profile,
            workflow=route.key.workflow,
            image_path=str(outcome.get("image_path") or ""),
            submitted_reference_ids=tuple(item.reference_id for item in references.submitted),
            degraded_capabilities=references.degraded_capabilities,
            generation_completed=bool(outcome.get("generation_completed") or outcome.get("image_path")),
            error_code=str(outcome.get("error_code") or ("" if outcome.get("image_path") else "submission_failed")),
            failure_stage=str(outcome.get("failure_stage") or ""),
            note=str(outcome.get("note") or ""),
            trace=tuple(trace),
        )


class LegacyCallbackAdapter:
    backend = "legacy"

    def __init__(self, callback: Callable[..., Awaitable[Mapping[str, Any]]], capabilities: BackendCapabilitiesV1 | None = None) -> None:
        self.callback = callback
        self._capabilities = capabilities or BackendCapabilitiesV1(text2img=True, edit=True, negative_prompt=False, max_reference_images=1, reference_roles=("identity", "outfit", "edit_source"), source="legacy_contract")

    async def capabilities(self, route: RouteDefinition) -> BackendCapabilitiesV1:
        return self._capabilities

    async def generate(self, route, spec, prompt, references: ReferencePlan, trace):
        outcome = await self.callback(spec=spec, prompt=prompt, references=references.submitted, route=route)
        return GenerationResultV1(
            request_id=spec.request_id, backend=self.backend, model_profile=prompt.model_profile,
            workflow=route.key.workflow, image_path=str(outcome.get("image_path") or ""),
            submitted_reference_ids=tuple(item.reference_id for item in references.submitted),
            error_code=str(outcome.get("error_code") or ""), note=str(outcome.get("note") or ""), trace=tuple(trace),
        )


@dataclass(slots=True)
class GenerationMetrics:
    counters: Counter = field(default_factory=Counter)
    latency_ms: list[int] = field(default_factory=list)

    def record(self, *, route: str, ok: bool, elapsed_ms: int, error_code: str = "") -> None:
        self.counters[f"route:{route}:total"] += 1
        self.counters[f"route:{route}:{'ok' if ok else 'error'}"] += 1
        if error_code:
            self.counters[f"error:{error_code}"] += 1
        self.latency_ms.append(max(0, int(elapsed_ms)))
        if len(self.latency_ms) > 1000:
            del self.latency_ms[:-1000]

    def snapshot(self) -> dict[str, Any]:
        ordered = sorted(self.latency_ms)
        percentile = lambda ratio: ordered[min(len(ordered) - 1, int(len(ordered) * ratio))] if ordered else 0
        return {"counters": dict(self.counters), "latency_ms": {"p50": percentile(0.5), "p95": percentile(0.95), "count": len(ordered)}}


def validate_output_image(path: str, *, max_bytes: int = 50 * 1024 * 1024) -> tuple[bool, str]:
    try:
        file = Path(path)
        size = file.stat().st_size
        header = file.read_bytes()[:16]
    except (OSError, ValueError):
        return False, "output path is unavailable"
    if size <= 0 or size > max_bytes:
        return False, "output size is invalid"
    signatures = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF", b"GIF8", b"WEBP")
    if not any(header.startswith(value) or value in header for value in signatures):
        return False, "output is not a recognized image"
    return True, "ok"


__all__ = [
    "redact_sensitive", "path_within_roots", "endpoint_capabilities", "endpoint_model_profile",
    "ComfyUIServiceAdapter", "OnlineEndpointAdapter", "LegacyCallbackAdapter", "GenerationMetrics",
    "validate_output_image",
]
