from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import astrbot_plugin_image_companion.main as image_main
from astrbot_plugin_image_companion.main import ImageCompanionExtensionAPI


VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"reference-bytes"


def test_reference_import_ids_match_private_companion_contract(tmp_path: Path) -> None:
    api = ImageCompanionExtensionAPI(SimpleNamespace(data_dir=str(tmp_path)))
    receipt = asyncio.run(
        api.import_references(
            {
                "assets": [{"content": VALID_PNG}],
            }
        )
    )

    assert receipt["status"] == "succeeded"
    assert re.fullmatch(r"reflease_[0-9a-f]{48}", receipt["lease_id"])
    assert re.fullmatch(r"ref_[0-9a-f]{48}", receipt["asset_ids"][0])
    leased_path = Path(api._reference_leases[receipt["lease_id"]]["paths"][0])
    assert leased_path.suffix == ".png"
    assert leased_path.read_bytes() == VALID_PNG
    assert api.release_reference_import(receipt["lease_id"]) is True


def test_reference_import_rejects_unknown_image_format(tmp_path: Path) -> None:
    api = ImageCompanionExtensionAPI(SimpleNamespace(data_dir=str(tmp_path)))

    receipt = asyncio.run(
        api.import_references({"assets": [{"content": b"not-an-image"}]})
    )

    assert receipt["status"] == "failed"
    assert receipt["error"] == {"code": "reference_import_failed"}


def test_capability_status_exposes_version_and_ready_endpoint_count(monkeypatch) -> None:
    class Runtime:
        def capability_status(self):
            return {
                "installed": True,
                "enabled": True,
                "available": True,
                "reason": "ready",
                "selected_backend": "external",
                "backup_external_note": "",
                "backends": {"external": True, "backup_external": True},
            }

        def _external_image_api_endpoint_queue(self, **_kwargs):
            return [
                {"name": "ready", "enabled": True},
                {"name": "incomplete", "enabled": True},
                {"name": "disabled", "enabled": False},
            ]

        @staticmethod
        def _external_image_api_endpoint_unavailable_note(endpoint):
            return "" if endpoint["name"] == "ready" else "incomplete"

    plugin = SimpleNamespace(
        enabled=True,
        _private_companion_api=lambda: object(),
        image_setting=lambda _name, default=None: default,
    )
    monkeypatch.setattr(image_main, "ImageGenerationRuntime", lambda *_args: Runtime())

    status = ImageCompanionExtensionAPI(plugin).capability_status(object())

    assert status["state"] == "ready"
    assert status["plugin_version"] == image_main.PLUGIN_VERSION
    assert status["status_schema_version"] == image_main.STATUS_SCHEMA_VERSION
    assert status["api_version"] == image_main.API_VERSION
    assert status["endpoint_count"] == 2
    assert status["ready_endpoint_count"] == 1


def test_generation_failure_code_keeps_actionable_stage() -> None:
    classify = ImageCompanionExtensionAPI._generation_failure_code

    assert classify({"backend": "在线图片 API", "note": "后端不可用或未配置"}) == (
        "backend_unavailable",
        "routing",
    )
    assert classify({"backend": "在线图片 API", "note": "provider timeout"}) == (
        "provider_timeout",
        "provider",
    )
    assert classify({"backend": "参考图", "note": "未找到身份参考图"}) == (
        "reference_unavailable",
        "reference",
    )
