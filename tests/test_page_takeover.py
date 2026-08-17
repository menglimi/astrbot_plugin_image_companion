# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_image_companion.main import ImageCompanionPlugin


def _plugin() -> ImageCompanionPlugin:
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.enabled = True
    plugin.reuse_private_companion_settings = False
    plugin.reuse_private_companion_assets = False
    plugin.generation_count = 0
    plugin.last_generation = {}
    return plugin


def test_status_exposes_runtime_takeover() -> None:
    plugin = _plugin()
    plugin._private_companion_api = lambda: object()

    assert plugin.status()["managed_by_private_companion"] is True
    assert plugin.status()["available"] is True
    assert plugin.status()["state"] == "managed"


def test_initialize_does_not_register_independent_page_api() -> None:
    async def run() -> None:
        calls = []
        plugin = _plugin()
        plugin.context = SimpleNamespace(register_web_api=lambda *args: calls.append(args))
        plugin._private_companion_api = lambda: object()

        await plugin.initialize()

        assert calls == []

    asyncio.run(run())


def test_status_requires_private_companion_host() -> None:
    plugin = _plugin()
    plugin._private_companion_api = lambda: None

    status = plugin.status()

    assert status["available"] is False
    assert status["state"] == "unavailable"
    assert status["reason"] == "private_companion_required"


def test_metadata_has_no_independent_page() -> None:
    metadata = (Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(encoding="utf-8")

    assert "pages:" not in metadata
    assert "此插件为“我会永远陪着你”插件的拓展程序，请到其拓展页进行配置。" in metadata
    assert "独立页面" in metadata
    page_root = Path(__file__).resolve().parents[1] / "pages" / "image-companion"
    assert not page_root.exists() or not any(page_root.iterdir())


def test_reference_catalog_writeback_is_persisted_in_image_config() -> None:
    class Config(dict):
        save_count = 0

        async def save(self) -> None:
            self.save_count += 1

    async def run() -> None:
        plugin = _plugin()
        plugin.config = Config(
            {
                "image": {
                    "photo_persona_reference_image_path": "",
                    "photo_generation_scene_presets": "",
                    "photo_reference_catalog": [
                        {
                            "id": "persona",
                            "kind": "persona",
                            "source": "https://example.invalid/persona.png",
                            "note": "identity",
                            "reference_roles": ["identity"],
                        },
                        {
                            "id": "sleepwear",
                            "kind": "library",
                            "source": "C:/cache/sleepwear.png",
                            "note": "sleepwear",
                            "reference_roles": ["identity", "outfit"],
                            "preferred_preset": "居家睡衣",
                        }
                    ],
                }
            }
        )

        saved = await plugin._set_photo_reference_config_path("C:/cache/persona.png")

        assert saved is True
        assert plugin.config.save_count == 1
        catalog = plugin.config["image"]["photo_reference_catalog"]
        assert catalog[0]["source"] == "C:/cache/persona.png"
        assert catalog[1]["preferred_preset"] == "居家睡衣"

    asyncio.run(run())
