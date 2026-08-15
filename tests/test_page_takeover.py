# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
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


def test_initialize_registers_status_page_api() -> None:
    async def run() -> None:
        calls = []
        plugin = _plugin()
        plugin.context = SimpleNamespace(register_web_api=lambda *args: calls.append(args))

        await plugin.initialize()

        assert calls[0][0] == "/astrbot_plugin_image_companion/page/status"
        assert calls[0][2] == ["GET"]

    asyncio.run(run())
