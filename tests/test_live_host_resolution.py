import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import astrbot_plugin_image_companion.main as image_main
from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime


def make_api(tmp_path, monkeypatch):
    plugin = SimpleNamespace()
    plugin.image_setting = MethodType(image_main.ImageCompanionPlugin.image_setting, plugin)
    plugin.config = {"image": dict(image_main._IMAGE_SETTING_DEFAULTS)}
    plugin.reuse_private_companion_settings = True
    plugin.reuse_private_companion_assets = True
    plugin.enabled = True
    plugin.data_dir = str(tmp_path)
    plugin.image_data_lock = asyncio.Lock()
    plugin._image_data = {}
    plugin.persist_image_state = AsyncMock()
    plugin._note_generation = Mock()
    host = SimpleNamespace(
        context=object(), data_dir=str(tmp_path), data={},
        photo_generation_backend="external",
    )
    private_api = SimpleNamespace(_plugin=host)
    plugin._private_companion_api = lambda: private_api
    seen = []
    image = tmp_path / "output.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    async def backend(runtime, prompt_text=""):
        # Exercise the exact attribute access that failed in production.
        seen.append((runtime.photo_generation_backend, runtime.context))
        return "external", str(image), "ok"

    monkeypatch.setattr(ImageGenerationRuntime, "_generate_photo_image_legacy", backend)
    monkeypatch.setattr(ImageGenerationRuntime, "_photo_generation_result_metadata", lambda *a, **k: {})
    return image_main.ImageCompanionExtensionAPI(plugin), private_api, host, seen


def test_formal_task_uses_legacy_backend_with_schema_defaults(tmp_path, monkeypatch):
    api, private_api, host, seen = make_api(tmp_path, monkeypatch)
    result = asyncio.run(api.execute_task({"input": {"prompt_text": "test"}}))
    assert result["status"] == "succeeded"
    assert seen == [("external", host.context)]


def test_formal_task_resolves_reloaded_host(tmp_path, monkeypatch):
    api, private_api, host, seen = make_api(tmp_path, monkeypatch)
    asyncio.run(api.execute_task({"input": {}}))
    replacement = SimpleNamespace(
        context=object(), data_dir=str(tmp_path), data={}, photo_generation_backend="comfyui",
    )
    private_api._plugin = replacement
    result = asyncio.run(api.execute_task({"input": {}}))
    assert result["status"] == "succeeded"
    assert seen == [("external", host.context), ("comfyui", replacement.context)]


def test_missing_host_returns_routing_failure(tmp_path, monkeypatch):
    api, private_api, host, seen = make_api(tmp_path, monkeypatch)
    private_api._plugin = None
    result = asyncio.run(api.execute_task({"input": {}}))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "backend_unavailable"
    assert seen == []
