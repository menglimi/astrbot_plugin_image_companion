from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime


class _SdgenPlugin:
    config = {}

    async def _check_webui_available(self):
        return True, "ok"

    async def _call_t2i_api(self, _prompt):
        async def updates():
            yield {"progress": 0.5}
            yield {
                "images": [
                    base64.b64encode(b"png-bytes").decode("ascii"),
                ]
            }

        return updates()


@pytest.mark.asyncio
async def test_sdgen_accepts_async_generator_response(monkeypatch):
    runtime = ImageGenerationRuntime.__new__(ImageGenerationRuntime)
    monkeypatch.setattr(runtime, "_find_sdgen_plugin", lambda: _SdgenPlugin())
    saved = {}

    async def save(image_bytes, *, session_key, ext):
        saved.update(bytes=image_bytes, session_key=session_key, ext=ext)
        return "C:/generated.png"

    monkeypatch.setattr(runtime, "_save_external_generated_image", save)

    assert await runtime._run_sdgen_photo_generation("a cat", session_key="s1") == (
        "C:/generated.png",
        "ok",
    )
    assert saved == {"bytes": b"png-bytes", "session_key": "s1", "ext": ".png"}
