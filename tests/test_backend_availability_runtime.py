from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runtime_loader import RUNTIME  # noqa: E402


class _OwnerMustNotBeQueried:
    def __getattr__(self, name):
        if name.endswith("_photo_available"):
            raise AssertionError(f"availability check leaked to owner: {name}")
        raise AttributeError(name)


def _runtime():
    runtime = RUNTIME.ImageGenerationRuntime.__new__(RUNTIME.ImageGenerationRuntime)
    runtime._image_service = SimpleNamespace()
    runtime._image_owner = _OwnerMustNotBeQueried()
    runtime.context = None
    return runtime


def test_external_availability_uses_runtime_endpoint_queue() -> None:
    runtime = _runtime()
    runtime._external_image_api_endpoint_queue = lambda **_kwargs: [
        {"name": "ready endpoint"}
    ]

    assert runtime._external_photo_available() is True

    runtime._external_image_api_endpoint_queue = lambda **_kwargs: []
    assert runtime._external_photo_available() is False


def test_external_availability_filters_incomplete_endpoints() -> None:
    runtime = _runtime()
    runtime.external_image_api_endpoints = [
        {
            "name": "ready endpoint",
            "platform": "auto",
            "base_url": "https://images.example/v1",
            "api_key": "secret",
            "model": "gpt-image-1",
            "enabled": True,
        }
    ]
    assert runtime._external_photo_available() is True

    runtime.external_image_api_endpoints = [
        {
            "name": "incomplete endpoint",
            "platform": "auto",
            "base_url": "https://images.example/v1",
            "api_key": "",
            "model": "gpt-image-1",
            "enabled": True,
        }
    ]
    assert runtime._external_photo_available() is False


def test_external_availability_falls_back_when_readiness_probe_raises() -> None:
    runtime = _runtime()

    def queue(**kwargs):
        if not kwargs:
            raise RuntimeError("readiness probe unavailable")
        return [
            {
                "name": "configured endpoint",
                "enabled": True,
                "base_url": "https://images.example/v1",
                "api_key": "secret",
                "model": "gpt-image-1",
            }
        ]

    runtime._external_image_api_endpoint_queue = queue

    assert runtime._external_photo_available() is True


def test_backend_summary_uses_existing_backup_availability_method() -> None:
    runtime = _runtime()
    runtime.external_image_api_endpoints = [{"name": "configured endpoint"}]
    runtime.photo_generation_backend = "auto"
    runtime._external_image_api_endpoint_queue = lambda **_kwargs: [
        {
            "name": "configured endpoint",
            "enabled": True,
            "base_url": "https://images.example/v1",
            "api_key": "secret",
            "model": "gpt-image-1",
        }
    ]
    runtime._comfyui_photo_available = lambda: False
    runtime._sdgen_photo_available = lambda: False
    runtime._custom_photo_tool_available = lambda: False
    runtime._backup_external_unavailable_note = lambda: "disabled"

    summary = runtime._photo_generation_backend_config_summary()

    assert "external_queue=1" in summary
    assert "backup_note=disabled" in summary


def test_backup_external_availability_uses_runtime_configuration() -> None:
    runtime = _runtime()
    runtime._backup_external_unavailable_note = lambda: ""
    assert runtime._backup_external_photo_available() is True

    runtime._backup_external_unavailable_note = lambda: "missing_backup_endpoints"
    assert runtime._backup_external_photo_available() is False


def test_local_and_tool_availability_checks_do_not_delegate_to_owner() -> None:
    runtime = _runtime()
    runtime.comfyui_text2img_workflow_name = "text2img"
    runtime.comfyui_selfie_workflow_name = ""
    runtime._get_comfyui_module = lambda: object()
    runtime._find_sdgen_plugin = lambda: object()
    runtime._find_custom_photo_tool_handler = lambda: object()

    assert runtime._comfyui_photo_available() is True
    assert runtime._sdgen_photo_available() is True
    assert runtime._custom_tool_photo_available() is True

    runtime._get_comfyui_module = lambda: None
    runtime._find_sdgen_plugin = lambda: None
    runtime._find_custom_photo_tool_handler = lambda: None

    assert runtime._comfyui_photo_available() is False
    assert runtime._sdgen_photo_available() is False
    assert runtime._custom_tool_photo_available() is False


def test_capability_status_and_execution_checks_share_availability_source() -> None:
    runtime = _runtime()
    runtime.photo_generation_backend = "external"
    runtime._external_image_api_endpoint_queue = lambda **_kwargs: [
        {"name": "ready endpoint"}
    ]
    runtime._backup_external_unavailable_note = lambda: "missing_backup_endpoints"
    runtime.comfyui_text2img_workflow_name = ""
    runtime.comfyui_selfie_workflow_name = ""
    runtime._find_sdgen_plugin = lambda: None
    runtime._find_custom_photo_tool_handler = lambda: None

    status = runtime.capability_status()

    assert status["available"] is True
    assert status["reason"] == "ready"
    assert status["backends"] == {
        "comfyui": False,
        "sdgen": False,
        "external": True,
        "backup_external": False,
        "tool_call": False,
    }
