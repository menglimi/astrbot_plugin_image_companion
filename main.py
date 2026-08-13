# -*- coding: utf-8 -*-
"""The standalone image-generation extension API for the companion series."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools, register

from .image_runtime import ImageGenerationRuntime, _IMAGE_SETTING_UNSET


PLUGIN_NAME = "astrbot_plugin_image_companion"
PLUGIN_VERSION = "0.1.0"
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

    def status(self) -> dict[str, Any]:
        return self._plugin.status()

    def capability_status(self, owner: Any) -> dict[str, Any]:
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
        if not self._plugin.enabled:
            return {"enabled": False, "available": False, "busy": False, "reason": "独立生图插件未启用"}
        return ImageGenerationRuntime(self._plugin, owner).local_load_state(force_refresh=force_refresh)

    async def maintenance(self, owner: Any) -> dict[str, Any]:
        if not self._plugin.enabled:
            return {}
        runtime = ImageGenerationRuntime(self._plugin, owner)
        return await runtime._maybe_cleanup_generated_photos(force=True)

    async def generate_for_companion(self, owner: Any, request: dict[str, Any]) -> dict[str, Any]:
        if not self._plugin.enabled:
            return {"handled": False, "reason": "disabled"}
        try:
            runtime = ImageGenerationRuntime(self._plugin, owner)
            backend, image_path, note = await runtime.generate(dict(request or {}))
        except Exception as exc:
            logger.exception("[ImageCompanion] 独立生图执行失败: error_type=%s", type(exc).__name__)
            return {
                "handled": True,
                "backend": "独立生图服务",
                "image_path": "",
                "note": "独立生图服务执行失败，请在排障页查看生图记录。",
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
        runtime = ImageGenerationRuntime(self._plugin, owner)
        runner = getattr(runtime, "_run_external_photo_generation_with_endpoint", None)
        if not callable(runner):
            return {"ok": False, "message": "独立生图运行时不支持在线 API 测试"}
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
    "我会画给你看：陪伴体系的独立生图、改图与参考图库服务。",
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
            logger.warning("[ImageCompanion] 读取独立生图状态失败: error_type=%s", type(exc).__name__)

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
                logger.warning("[ImageCompanion] 保存独立生图状态失败: error_type=%s", type(exc).__name__)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": "compatibility_migration" if self.reuse_private_companion_settings else "independent",
            "reuse_private_companion_settings": self.reuse_private_companion_settings,
            "reuse_private_companion_assets": self.reuse_private_companion_assets,
            "generation_count": self.generation_count,
            "last_generation": copy.deepcopy(self.last_generation),
        }

    async def terminate(self) -> None:
        global _active_plugin
        await self.persist_image_state()
        if _active_plugin is self:
            _active_plugin = None
