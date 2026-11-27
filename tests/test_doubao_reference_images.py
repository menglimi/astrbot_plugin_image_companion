# -*- coding: utf-8 -*-
"""豆包/火山方舟 Seedream 参考图提交的回归测试。"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime

PNG_BYTES = b"\x89PNG\r\n\x1a\nreference"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


class DoubaoReferenceImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.reference = self.directory / "reference.png"
        self.reference.write_bytes(PNG_BYTES)
        self.runtime = ImageGenerationRuntime.__new__(ImageGenerationRuntime)
        self.runtime.external_image_api_platform = "doubao"
        self.runtime.external_image_api_base_url = ARK_BASE_URL
        self.runtime.external_image_api_key = "test-key"
        self.runtime.external_image_api_model = "doubao-seedream-5.0-flash"
        self.runtime.external_image_api_size = "2K"
        self.runtime.external_image_api_timeout_seconds = 20
        self.runtime.external_image_api_custom_headers = ""
        self.runtime._image_service = SimpleNamespace(
            config={},
            image_setting=lambda name, default=None: default,
            _extract_json_payload=json.loads,
        )
        self.runtime._image_owner = SimpleNamespace()
        self.runtime._extract_json_payload = json.loads
        self.runtime._append_photo_generation_http_exchange = lambda **kwargs: None
        self.runtime._external_image_diagnostic_text = lambda value, limit=220: str(value)[:limit]
        self.runtime._materialize_external_image_value = AsyncMock(return_value=("out.png", "ok"))

    def build_session(self, body: dict) -> MagicMock:
        session = MagicMock()
        session.__aenter__.return_value = session
        response = MagicMock(status=200, headers={"Content-Type": "application/json"})
        response.__aenter__.return_value = response
        response.text = AsyncMock(return_value=json.dumps(body))
        session.post.return_value = response
        return session

    async def test_reference_image_is_submitted_as_data_url(self) -> None:
        session = self.build_session({"data": [{"url": "https://images.example.test/out.png"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            result = await self.runtime._run_doubao_photo_generation(
                "same person, sitting by the window",
                session_key="test",
                reference_image_path=str(self.reference),
            )

        self.assertEqual("out.png", result[0], result[1])
        self.assertEqual(ARK_BASE_URL + "/images/generations", session.post.call_args.args[0])
        payload = session.post.call_args.kwargs["json"]
        expected = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        self.assertEqual([expected], payload["image"])

    async def test_text_to_image_keeps_payload_without_image_field(self) -> None:
        session = self.build_session({"data": [{"url": "https://images.example.test/out.png"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            await self.runtime._run_doubao_photo_generation("a quiet classroom", session_key="test")

        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("image", payload)

    async def test_reference_images_are_deduplicated_and_capped_at_four(self) -> None:
        references = []
        for index in range(6):
            path = self.directory / f"ref{index}.png"
            path.write_bytes(PNG_BYTES + bytes([index]))
            references.append(str(path))
        session = self.build_session({"data": [{"url": "https://images.example.test/out.png"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            await self.runtime._run_doubao_photo_generation(
                "prompt",
                session_key="test",
                reference_image_path=references[0],
                reference_image_paths=tuple([references[0], *references]),
            )

        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(4, len(payload["image"]))
        self.assertTrue(all(item.startswith("data:image/png;base64,") for item in payload["image"]))

    async def test_unreadable_reference_stops_instead_of_falling_back(self) -> None:
        missing = self.directory / "missing.png"
        session = self.build_session({"data": [{"url": "https://images.example.test/out.png"}]})
        with patch("aiohttp.ClientSession", return_value=session):
            image_path, note = await self.runtime._run_doubao_photo_generation(
                "prompt",
                session_key="test",
                reference_image_path=str(missing),
            )

        self.assertEqual("", image_path)
        self.assertIn("参考图", note)
        session.post.assert_not_called()

    async def test_doubao_dispatch_forwards_reference_images(self) -> None:
        runner = AsyncMock(return_value=("out.png", "ok"))
        with (
            patch.object(ImageGenerationRuntime, "_run_doubao_photo_generation", runner),
            patch.object(ImageGenerationRuntime, "_coerce_external_photo_generation_outcome", lambda self, value: value),
        ):
            await self.runtime._run_external_photo_generation_once(
                "prompt",
                session_key="test",
                reference_image_path=str(self.reference),
            )

        runner.assert_awaited_once()
        self.assertEqual(str(self.reference), runner.await_args.kwargs["reference_image_path"])
        self.assertEqual((str(self.reference),), runner.await_args.kwargs["reference_image_paths"])

    def test_doubao_endpoint_declares_multi_reference_capacity(self) -> None:
        endpoint = {"platform": "doubao", "model": "doubao-seedream-5.0-flash", "base_url": ARK_BASE_URL}

        self.assertEqual(4, self.runtime._external_image_endpoint_multi_reference_capacity(endpoint, 6))
        self.assertEqual(1, self.runtime._external_image_endpoint_multi_reference_capacity(endpoint, 1))
