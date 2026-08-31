# -*- coding: utf-8 -*-
"""Image-generation extension API owned by the private companion host."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools, register

from .helpers import _set_into_config
from .image_runtime import ImageGenerationRuntime, _IMAGE_SETTING_UNSET
from .generation_config import active_engine_claims_profile, route_diagnostics
from .photo_reference_catalog import CATALOG_VERSION, load_catalog, validate_and_serialize


PLUGIN_NAME = "astrbot_plugin_image_companion"
PLUGIN_VERSION = "0.3.5"
_active_plugin: "ImageCompanionPlugin | None" = None

_IMAGE_SETTING_DEFAULTS = {
    "photo_generation_backend": "auto",
    "external_image_api_platform": "auto",
    "external_image_api_base_url": "",
    "external_image_api_key": "",
    "external_image_api_model": "",
    "external_image_api_size": "1024x1024",
    "external_image_api_timeout_seconds": 180,
    "external_image_api_custom_headers": "",
    "external_image_api_endpoints": [],
    "enable_backup_external_image_api": False,
    "backup_external_image_api_platform": "auto",
    "backup_external_image_api_base_url": "",
    "backup_external_image_api_key": "",
    "backup_external_image_api_model": "",
    "comfyui_text2img_workflow_name": "",
    "comfyui_selfie_workflow_name": "",
    "comfyui_photo_wait_seconds": 90,
    "custom_photo_tool_name": "",
    "custom_photo_tool_prompt_param": "prompt",
    "custom_photo_tool_kind_param": "",
    "custom_photo_tool_reference_param": "",
    "enable_photo_reference_image": False,
    "photo_persona_reference_image_path": "",
    "photo_reference_library": [],
    "photo_reference_catalog": [],
    "photo_generation_prompt_format": "traditional",
    "photo_generation_style": "真实",
    "photo_generation_style_custom_prompt": "",
    "photo_generation_negative_prompt_mode": "safe_default",
    "photo_generation_negative_prompt": "",
    "photo_generation_text2img_negative_prompt": "",
    "photo_generation_selfie_negative_prompt": "",
    "photo_generation_edit_negative_prompt": "",
    "photo_generation_fixed_prompt": "",
    "photo_generation_text2img_fixed_prompt": "",
    "photo_generation_selfie_fixed_prompt": "",
    "photo_generation_edit_fixed_prompt": "",
    "photo_generation_scene_presets": "",
    "enable_generated_photo_cleanup": True,
    "generated_photo_retention_days": 30,
    "generated_photo_max_mb": 512,
    "enable_local_photo_load_guard": True,
    "local_photo_cpu_busy_percent": 85,
    "local_photo_memory_busy_percent": 88,
}


def _new_reference_token() -> str:
    """Return the 48-hex token required by the companion image contract."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:16]


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"true", "1", "yes", "on", "开启", "启用"}:
            return True
        if value in {"false", "0", "no", "off", "关闭", "停用", ""}:
            return False
    return default if value is None else bool(value)


def get_image_companion_api() -> Any | None:
    plugin = _active_plugin
    return getattr(plugin, "extension_api", None) if plugin is not None else None


class ImageCompanionExtensionAPI:
    """Stable image-service boundary used by companion-family plugins.

    The request and response preserve the former private companion generator
    contract. This keeps all existing command, tool and proactive call sites
    compatible while the executor is migrated here in staged releases.
    """

    def __init__(self, plugin: "ImageCompanionPlugin") -> None:
        self._plugin = plugin
        self._instance_generation = int(time.time_ns() % 2_000_000_000) or 1
        self._reference_leases: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {
            "plugin_id": PLUGIN_NAME,
            "instance_generation": self._instance_generation,
            "api_family": "image.generation",
            "api_version": "image.generation-api.v1",
            "supported_task_versions": ["image.task.v1"],
            "capabilities": [
                "image.build-task", "image.validate-task", "image.import-references",
                "image.release-reference-import", "image.execute-task", "image.execute-task.active",
            ],
            "lifecycle_state": "ready",
            "degraded_reasons": [],
        }

    def versions(self) -> dict[str, Any]:
        return {
            "plugin_id": PLUGIN_NAME,
            "instance_generation": self._instance_generation,
            "api_family": "image.generation",
            "api_version": "image.generation-api.v1",
            "task_version": "image.task.v1",
            "supported_task_versions": ["image.task.v1"],
        }

    def build_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload or {})
        return {
            "version": "image.task.v1",
            "operation": "generate",
            "workflow_kind": str(value.get("workflow_kind") or "text2img"),
            "input": value,
            "instance_generation": self._instance_generation,
        }

    def validate_task(self, task: dict[str, Any]) -> dict[str, Any]:
        valid = (
            isinstance(task, dict) and task.get("version") == "image.task.v1"
            and task.get("operation") == "generate"
            and isinstance(task.get("workflow_kind"), str)
            and isinstance(task.get("input"), dict)
            and task.get("instance_generation") == self._instance_generation
        )
        return {"valid": valid, "task_version": "image.task.v1", "operation": "generate", "workflow_kind": str(task.get("workflow_kind") or "") if isinstance(task, dict) else ""}

    async def import_references(self, payload: dict[str, Any]) -> dict[str, Any]:
        assets = payload.get("assets") if isinstance(payload, dict) else None
        base = {"result_version": "image.reference-import-result.v1", "instance_generation": self._instance_generation, "ttl_seconds": 90}
        if not isinstance(assets, list) or not assets or len(assets) > 4:
            return {**base, "status": "failed", "lease_id": None, "asset_ids": [], "error": {"code": "reference_import_invalid"}}
        root = Path(self._plugin.data_dir) / "reference_imports"
        root.mkdir(parents=True, exist_ok=True)
        lease_id = "reflease_" + _new_reference_token()
        asset_ids: list[str] = []
        paths: list[str] = []
        try:
            for item in assets:
                content = item.get("content") if isinstance(item, dict) else None
                if not isinstance(content, (bytes, bytearray)) or not content:
                    raise ValueError("invalid reference")
                asset_id = "ref_" + _new_reference_token()
                path = root / f"{asset_id}.bin"
                path.write_bytes(bytes(content))
                asset_ids.append(asset_id)
                paths.append(str(path))
            self._reference_leases[lease_id] = {"asset_ids": asset_ids, "paths": paths}
            return {**base, "status": "succeeded", "lease_id": lease_id, "asset_ids": asset_ids, "error": None}
        except Exception:
            for path in paths:
                try: Path(path).unlink(missing_ok=True)
                except OSError: pass
            return {**base, "status": "failed", "lease_id": None, "asset_ids": [], "error": {"code": "reference_import_failed"}}

    def release_reference_import(self, lease_id: str) -> bool:
        lease = self._reference_leases.pop(str(lease_id or ""), None)
        if not isinstance(lease, dict): return False
        for path in lease.get("paths", []):
            try: Path(path).unlink(missing_ok=True)
            except OSError: pass
        return True

    async def execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        request = dict(task.get("input") or {}) if isinstance(task, dict) else {}
        asset_ids = request.get("reference_asset_ids") or []
        for lease in self._reference_leases.values():
            if set(asset_ids).issubset(set(lease.get("asset_ids", []))):
                request["reference_image_paths"] = [path for asset_id, path in zip(lease.get("asset_ids", []), lease.get("paths", [])) if asset_id in asset_ids]
                break
        owner = getattr(self, "_companion_owner", None)
        outcome = await self.generate_for_companion(owner, request)
        request_id = uuid.uuid4().hex
        image_path = str(outcome.get("image_path") or "")
        if image_path and os.path.isfile(image_path):
            raw = Path(image_path).read_bytes()
            return {"result_version": "image.result.v1", "task_version": "image.task.v1", "request_id": request_id, "status": "succeeded", "backend": "comfyui" if str(outcome.get("backend") or "").lower() == "comfyui" else "external", "backend_task_id": request_id, "output": {"asset_id": "image_" + request_id[:32], "kind": "image", "media_type": "image/png" if raw.startswith(b"\x89PNG") else "image/jpeg", "local_path": image_path, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}, "error": None, "degraded_capabilities": []}
        return {"result_version": "image.result.v1", "task_version": "image.task.v1", "request_id": request_id, "status": "failed", "backend": "", "backend_task_id": "", "output": None, "error": {"code": "generation_failed", "stage": "execute"}, "degraded_capabilities": []}

    def status(self) -> dict[str, Any]:
        return self._plugin.status()

    def debug_data_dirs(self, owner: Any = None) -> list[str]:
        """Return the data roots used by the image runtime's debug recorder.

        The split plugin can either reuse the companion data directory or own
        a separate one.  Exposing the resolved candidates keeps the companion
        page from guessing which root contains the current trace.
        """
        owner_dir = str(getattr(owner, "data_dir", "") or "").strip()
        service_dir = str(getattr(self._plugin, "data_dir", "") or "").strip()
        candidates = []
        if self._plugin.reuse_private_companion_assets and owner_dir:
            candidates.append(owner_dir)
        if service_dir:
            candidates.append(service_dir)
        if owner_dir:
            candidates.append(owner_dir)
        return list(dict.fromkeys(candidates))

    def _host_available(self) -> bool:
        return self._plugin._private_companion_api() is not None

    def generation_route_diagnostics(self) -> dict[str, Any]:
        """Return redacted route validation and a non-mutating migration preview."""
        return route_diagnostics(self._plugin.config)

    def claims_model_profile(self, model_profile: str, *, operation: str = "") -> bool:
        """Tell companion bridges when the active unified engine owns a route."""
        return active_engine_claims_profile(
            getattr(self._plugin, "config", {}) or {},
            model_profile,
            operation=operation,
        )

    def _comfyui_service(self, owner: Any) -> Any:
        service = ImageGenerationRuntime(self._plugin, owner)._get_comfyui_public_service()
        if service is None:
            raise RuntimeError("ComfyUI public service is unavailable")
        return service

    def list_comfyui_workflows(self, owner: Any) -> list[dict[str, Any]]:
        """Return structured workflow capabilities for an advanced settings UI."""
        return self._comfyui_service(owner).list_workflows()

    def inspect_comfyui_workflow(self, owner: Any, workflow_id: str) -> dict[str, Any]:
        return self._comfyui_service(owner).inspect_workflow(workflow_id)

    def validate_comfyui_mapping(
        self,
        owner: Any,
        workflow_id: str,
        mapping: dict[str, Any],
        *,
        save: bool = False,
    ) -> dict[str, Any]:
        return self._comfyui_service(owner).validate_mapping(workflow_id, mapping, save=save)

    def capability_status(self, owner: Any) -> dict[str, Any]:
        if not self._host_available():
            return {
                "installed": True,
                "enabled": self._plugin.enabled,
                "available": False,
                "reason": "private_companion_required",
                "selected_backend": str(self._plugin.image_setting("photo_generation_backend", "auto") or "auto"),
                "backup_external_note": "private_companion_required",
                "backends": {},
            }
        if not self._plugin.enabled:
            return {
                "installed": True,
                "enabled": False,
                "available": False,
                "reason": "disabled",
                "selected_backend": str(self._plugin.image_setting("photo_generation_backend", "auto") or "auto"),
                "backup_external_note": "disabled",
                "backends": {},
            }
        return ImageGenerationRuntime(self._plugin, owner).capability_status()

    def local_load_state(self, owner: Any, *, force_refresh: bool = False) -> dict[str, Any]:
        if not self._host_available():
            return {"enabled": self._plugin.enabled, "available": False, "busy": False, "reason": "private_companion_required"}
        if not self._plugin.enabled:
            return {"enabled": False, "available": False, "busy": False, "reason": "生图扩展未启用"}
        return ImageGenerationRuntime(self._plugin, owner).local_load_state(force_refresh=force_refresh)

    async def maintenance(self, owner: Any) -> dict[str, Any]:
        if not self._plugin.enabled or not self._host_available():
            return {}
        runtime = ImageGenerationRuntime(self._plugin, owner)
        return await runtime._maybe_cleanup_generated_photos(force=True)

    async def generate_for_companion(self, owner: Any, request: dict[str, Any]) -> dict[str, Any]:
        if not self._host_available():
            return {"handled": False, "reason": "private_companion_required"}
        if not self._plugin.enabled:
            return {"handled": False, "reason": "disabled"}
        try:
            runtime = ImageGenerationRuntime(self._plugin, owner)
            backend, image_path, note = await runtime.generate(dict(request or {}))
        except Exception as exc:
            logger.exception("[ImageCompanion] 生图扩展执行失败: error_type=%s", type(exc).__name__)
            return {
                "handled": True,
                "backend": "生图扩展",
                "image_path": "",
                "note": "生图扩展执行失败，请在排障页查看生图记录。",
            }
        self._plugin._note_generation(backend, image_path, note, request)
        metadata = runtime._photo_generation_result_metadata(
            image_path=image_path,
            session_key=str((request or {}).get("session_key") or ""),
        )
        await self._plugin.persist_image_state()
        return {
            "handled": True,
            "backend": backend,
            "image_path": image_path,
            "note": note,
            "metadata": metadata,
        }

    async def test_endpoint(self, owner: Any, endpoint: dict[str, Any], prompt: str) -> dict[str, Any]:
        """Run the existing endpoint diagnostic through the split runtime."""
        if not self._host_available():
            return {"ok": False, "message": "请先安装并启用“我会永远陪着你”"}
        runtime = ImageGenerationRuntime(self._plugin, owner)
        runner = getattr(runtime, "_run_external_photo_generation_with_endpoint", None)
        if not callable(runner):
            return {"ok": False, "message": "生图扩展运行时不支持在线 API 测试"}
        outcome = await runner(
            dict(endpoint or {}),
            str(prompt or "a simple test image"),
            session_key="image_companion:endpoint_test",
        )
        normalized = runtime._coerce_external_photo_generation_outcome(outcome)
        return {
            "ok": bool(normalized.image_path),
            "image_path": normalized.image_path,
            "message": normalized.note,
        }


@register(
    PLUGIN_NAME,
    "menglimi",
    "我会画给你看：陪伴体系的生图、改图与参考图库服务。",
    PLUGIN_VERSION,
)
class ImageCompanionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        global _active_plugin
        super().__init__(context)
        self.context = context
        self.config = config
        self.enabled = _as_bool(config.get("enabled", True), True)
        self.reuse_private_companion_settings = _as_bool(
            self._cfg("migration.reuse_private_companion_settings", True), True
        )
        self.reuse_private_companion_assets = _as_bool(
            self._cfg("migration.reuse_private_companion_assets", True), True
        )
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._image_data: dict[str, dict[str, Any]] = {}
        self.image_data_lock = asyncio.Lock()
        self._image_state_path = Path(self.data_dir) / "image_generation_state.json"
        self._load_image_state()
        self.generation_count = 0
        self.last_generation: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.extension_api = ImageCompanionExtensionAPI(self)
        _active_plugin = self

    async def initialize(self) -> None:
        if self._private_companion_api() is None:
            logger.warning("[ImageCompanion] 未检测到陪伴主插件，生图扩展保持不可用")
        else:
            logger.info("[ImageCompanion] 已接入陪伴主插件，页面与运行入口由陪伴面板统一管理")

    def _private_companion_api(self) -> Any | None:
        module_names = (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        )
        suffixes = tuple(name.removeprefix("data.plugins.") for name in module_names)
        modules = [sys.modules.get(name) for name in module_names]
        modules.extend(
            module
            for name, module in list(sys.modules.items())
            if module is not None and any(name.endswith(suffix) for suffix in suffixes)
        )
        for module in modules:
            if module is None:
                continue
            getter = getattr(module, "get_private_companion_api", None)
            try:
                api = getter() if callable(getter) else None
            except Exception:
                api = None
            if api is not None:
                return api
        getter = getattr(self.context, "get_registered_star", None)
        if callable(getter):
            try:
                metadata = getter("astrbot_plugin_private_companion")
                instance = getattr(metadata, "star_cls", None) if metadata is not None else None
                return getattr(instance, "extension_api", None)
            except Exception:
                pass
        return None

    def _cfg(self, key: str, default: Any) -> Any:
        if key in self.config:
            return self.config.get(key, default)
        value: Any = self.config
        for part in key.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value

    def _note_generation(self, backend: Any, image_path: Any, note: Any, request: dict[str, Any]) -> None:
        self.generation_count += 1
        self.last_generation = {
            "workflow_kind": str(request.get("workflow_kind") or ""),
            "backend": str(backend or ""),
            "success": bool(image_path),
            "note": str(note or "")[:500],
        }

    def image_setting(self, name: str, default: Any = _IMAGE_SETTING_UNSET) -> Any:
        """Read split-plugin values first, then the installed legacy values.

        Configuration keys intentionally retain their former flat names.  This
        lets AstrBot users move one setting at a time and preserves every
        existing backend contract during the transition.
        """
        image_config = self.config.get("image")
        if isinstance(image_config, dict) and name in image_config:
            value = image_config.get(name)
            # Schema defaults must not silently replace an existing setup on
            # upgrade. A non-default value is an explicit handoff to this
            # plugin; otherwise the owner remains the migration source.
            if not (
                self.reuse_private_companion_settings
                and name in _IMAGE_SETTING_DEFAULTS
                and value == _IMAGE_SETTING_DEFAULTS[name]
            ):
                return value
        if name in self.config:
            return self.config.get(name)
        return default

    async def _save_config_if_possible(self) -> bool:
        for method_name in ("save_config", "save", "save_conf"):
            save = getattr(self.config, method_name, None)
            if not callable(save):
                continue
            try:
                result = save()
                if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                    await result
                return True
            except TypeError:
                continue
            except Exception as exc:
                logger.warning(
                    "[ImageCompanion] 自动保存配置失败: error_type=%s",
                    type(exc).__name__,
                )
                return False
        logger.warning("[ImageCompanion] 当前配置对象没有可用保存方法，本次修改未落盘")
        return False

    async def _set_image_config_value(self, name: str, value: Any) -> bool:
        previous = copy.deepcopy(self.image_setting(name, _IMAGE_SETTING_UNSET))
        if not _set_into_config(
            self.config,
            name,
            value,
            allow_flat_fallback=False,
        ):
            logger.warning("[ImageCompanion] 生图配置项不存在，拒绝回写: key=%s", name)
            return False
        if await self._save_config_if_possible():
            return True
        if previous is not _IMAGE_SETTING_UNSET:
            _set_into_config(
                self.config,
                name,
                previous,
                allow_flat_fallback=False,
            )
        return False

    def _photo_reference_preset_names(self) -> tuple[str, ...]:
        presets = ImageGenerationRuntime._builtin_photo_generation_scene_presets(self)
        presets.update(
            ImageGenerationRuntime._parse_photo_generation_scene_presets(
                self,
                self.image_setting("photo_generation_scene_presets", ""),
            )
        )
        return tuple(presets)

    async def _set_photo_reference_catalog_config(self, items: Any) -> bool:
        try:
            serialized = validate_and_serialize(
                items,
                preset_names=self._photo_reference_preset_names(),
            )
        except Exception as exc:
            logger.warning(
                "[ImageCompanion] 保存结构化参考图库失败: error_type=%s",
                type(exc).__name__,
            )
            return False
        return await self._set_image_config_value(
            "photo_reference_catalog",
            serialized,
        )

    async def _set_photo_reference_config_path(self, path: str) -> bool:
        clean = str(path or "").strip()[:1000]
        raw_catalog = self.image_setting(
            "photo_reference_catalog",
            _IMAGE_SETTING_UNSET,
        )
        if raw_catalog is _IMAGE_SETTING_UNSET:
            return await self._set_image_config_value(
                "photo_persona_reference_image_path",
                clean,
            )
        try:
            loaded = load_catalog(
                raw_catalog,
                catalog_version=CATALOG_VERSION,
                preset_names=self._photo_reference_preset_names(),
            )
            updated = tuple(
                replace(item, source=clean)
                if item.kind == "persona"
                else item
                for item in loaded.references
            )
        except Exception as exc:
            logger.warning(
                "[ImageCompanion] 更新人设参考图失败: error_type=%s",
                type(exc).__name__,
            )
            return False
        return await self._set_photo_reference_catalog_config(updated)

    def image_data_for(self, owner: Any) -> dict[str, Any]:
        """Keep generated image state in this plugin, with read-through context.

        Conversation, schedules and users remain owned by the caller.  Image
        archives, traces and reference metadata are copied lazily so an upgrade
        retains the current experience without continuing to mutate the old
        plugin's image store.
        """
        owner_key = str(getattr(owner, "data_dir", "") or id(owner))
        state = self._image_data.get(owner_key)
        if state is not None:
            return state
        source = getattr(owner, "data", {})
        source = source if isinstance(source, dict) else {}
        state = dict(source)
        for key in (
            "recent_photo_generations",
            "recent_photo_continuity",
            "daily_outfit_photo",
            "daily_outfit_history",
            "photo_reference_assets",
        ):
            if key in source:
                state[key] = copy.deepcopy(source[key])
        self._image_data[owner_key] = state
        return state

    def _load_image_state(self) -> None:
        try:
            raw = json.loads(self._image_state_path.read_text(encoding="utf-8"))
            stored = raw.get("owners") if isinstance(raw, dict) else None
            if isinstance(stored, dict):
                self._image_data = {
                    str(key): value for key, value in stored.items() if isinstance(value, dict)
                }
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("[ImageCompanion] 读取生图扩展状态失败: error_type=%s", type(exc).__name__)

    def _save_image_state_sync(self) -> None:
        self._image_state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._image_state_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps({"version": 1, "owners": self._image_data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self._image_state_path)

    async def persist_image_state(self) -> None:
        async with self.image_data_lock:
            try:
                await asyncio.to_thread(self._save_image_state_sync)
            except Exception as exc:
                logger.warning("[ImageCompanion] 保存生图扩展状态失败: error_type=%s", type(exc).__name__)

    def status(self) -> dict[str, Any]:
        metrics = getattr(self, "generation_metrics", None)
        managed = self._private_companion_api() is not None
        config = getattr(self, "config", {})
        raw_debug = config.get("debug") if isinstance(config, dict) and isinstance(config.get("debug"), dict) else {}
        if not raw_debug and isinstance(config, dict) and isinstance(config.get("image"), dict):
            raw_debug = config["image"].get("debug") if isinstance(config["image"].get("debug"), dict) else {}
        legacy_trace_enabled = False
        if not raw_debug:
            try:
                image_setting = getattr(self, "image_setting", None)
                legacy_trace_enabled = bool(
                    callable(image_setting)
                    and int(image_setting("photo_generation_trace_max_size_kb", 0) or 0) > 0
                )
            except Exception:
                legacy_trace_enabled = False
        debug_mode = str(
            raw_debug.get("capture_mode", raw_debug.get("mode", "redacted" if legacy_trace_enabled else "off"))
            or ("redacted" if legacy_trace_enabled else "off")
        ).strip().lower()
        raw_debug_enabled = raw_debug.get("enabled", legacy_trace_enabled)
        debug_enabled = (
            raw_debug_enabled.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            if isinstance(raw_debug_enabled, str)
            else bool(raw_debug_enabled)
        )
        raw_include_secrets = raw_debug.get("include_secrets", False)
        include_secrets = (
            raw_include_secrets.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            if isinstance(raw_include_secrets, str)
            else bool(raw_include_secrets)
        )
        debug_status = {
            "enabled": debug_enabled and debug_mode != "off",
            "capture_mode": debug_mode,
            "include_secrets": include_secrets,
            "sensitive": debug_mode == "full_with_secrets" and include_secrets,
            "legacy_compatibility": legacy_trace_enabled,
        }
        return {
            "installed": True,
            "enabled": self.enabled,
            "available": bool(self.enabled and managed),
            "managed_by_private_companion": managed,
            "state": "managed" if managed else "unavailable",
            "reason": "" if managed else "private_companion_required",
            "reuse_private_companion_settings": self.reuse_private_companion_settings,
            "reuse_private_companion_assets": self.reuse_private_companion_assets,
            "generation_count": self.generation_count,
            "last_generation": copy.deepcopy(self.last_generation),
            "debug": debug_status,
            "unified_engine": route_diagnostics(config if isinstance(config, dict) else {}),
            "metrics": metrics.snapshot() if callable(getattr(metrics, "snapshot", None)) else {},
        }

    async def terminate(self) -> None:
        global _active_plugin
        await self.persist_image_state()
        if _active_plugin is self:
            _active_plugin = None
