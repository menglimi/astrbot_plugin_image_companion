from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime


def test_generate_filters_image_task_metadata_for_legacy_executor() -> None:
    calls: list[dict[str, object]] = []

    class Runtime(ImageGenerationRuntime):
        async def _generate_photo_image_legacy(
            self,
            *,
            workflow_kind: str,
            prompt_text: str,
            session_key: str,
            reference_image_paths=(),
        ):
            calls.append(
                {
                    "workflow_kind": workflow_kind,
                    "prompt_text": prompt_text,
                    "session_key": session_key,
                    "reference_image_paths": reference_image_paths,
                }
            )
            return "legacy", "", "ok"

    runtime = Runtime.__new__(Runtime)
    runtime._image_service = SimpleNamespace()
    request = {
        "workflow_kind": "selfie",
        "prompt_text": "a portrait",
        "session_key": "private:user-a",
        "reference_image_paths": ["C:/reference.png"],
        "owner_id": "astrbot_plugin_private_companion",
        "scope": "private",
        "privacy": "private",
        "limits": {"image_size": "1024x1024"},
        "reference_asset_ids": ["ref_a"],
    }

    import asyncio

    assert asyncio.run(runtime.generate(request)) == ("legacy", "", "ok")
    assert calls == [
        {
            "workflow_kind": "selfie",
            "prompt_text": "a portrait",
            "session_key": "private:user-a",
            "reference_image_paths": ["C:/reference.png"],
        }
    ]
