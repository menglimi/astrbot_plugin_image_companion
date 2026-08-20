from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from generation_debug import GenerationDebugConfig, GenerationDebugRecorder  # noqa: E402
from _runtime_loader import RUNTIME  # noqa: E402


class _Harness(RUNTIME.ProactiveMessageMixin):
    def __init__(self, data_dir: str, recorder: GenerationDebugRecorder) -> None:
        self.data_dir = data_dir
        self.photo_generation_trace_max_size_kb = 0
        self.photo_generation_trace_backup_count = 0
        self._recorder = recorder
        self._image_service = types.SimpleNamespace(config={"debug": {}}, data_dir=data_dir)

    def _generation_debug_recorder(self, _config):
        return self._recorder

    def _photo_generation_trace_max_bytes(self):
        return 0


class _DownloadFailureHarness(_Harness):
    external_image_api_platform = "openai"
    external_image_api_base_url = "https://provider.test/v1"
    external_image_api_key = "header-secret"
    external_image_api_custom_headers = ""
    external_image_api_timeout_seconds = 20
    external_image_download_use_environment_proxy = False
    external_image_download_proxy = ""

    async def _get_external_image_download_session(self, *, use_environment_proxy):
        raise RuntimeError("injected download failure")


class HttpPayloadCaptureTests(unittest.TestCase):
    def test_full_with_secrets_captures_http_request_and_response_sidecars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(
                    enabled=True,
                    capture_mode="full_with_secrets",
                    include_secrets=True,
                    capture_payloads=True,
                ),
            )
            harness = _Harness(temp_dir, recorder)
            harness._append_photo_generation_trace_event(
                "trace-http",
                "request_received",
                context={"session": "private:test"},
            )
            harness._append_photo_generation_http_exchange(
                method="POST",
                endpoint="https://provider.test/v1/images?token=query-secret",
                request_headers={"Authorization": "Bearer header-secret", "X-Test": "yes"},
                request_body={"model": "image-model", "prompt": "hello", "api_key": "body-secret"},
                response_status=202,
                response_headers={"Content-Type": "application/json"},
                response_body='{"task_id":"task-123","status":"queued"}',
                stage="http_submit",
                task_id="task-123",
                session_key="private:test",
                model="image-model",
            )
            events = recorder.read_events(trace_id="trace-http")
            event = next(item for item in events if item["stage"] == "http_submit")
            self.assertEqual(202, event["data"]["http_status"])
            self.assertEqual("task-123", event["data"]["task_id"])
            request_meta = event["data"]["payloads"]["request"]
            response_meta = event["data"]["payloads"]["response"]
            request_path = pathlib.Path(temp_dir) / "photo_debug" / request_meta["path"]
            response_path = pathlib.Path(temp_dir) / "photo_debug" / response_meta["path"]
            self.assertIn("body-secret", request_path.read_text(encoding="utf-8"))
            self.assertIn("header-secret", request_path.read_text(encoding="utf-8"))
            self.assertIn("task-123", response_path.read_text(encoding="utf-8"))

    def test_redacted_http_payload_masks_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(
                    enabled=True,
                    capture_mode="full",
                    capture_payloads=True,
                ),
            )
            harness = _Harness(temp_dir, recorder)
            harness._append_photo_generation_trace_event("trace-redact", "request_received")
            harness._append_photo_generation_http_exchange(
                method="GET",
                endpoint="https://provider.test/task?token=query-secret",
                request_headers={"Authorization": "Bearer header-secret"},
                request_body={"token": "body-secret"},
                response_status=200,
                response_body='{"status":"running"}',
                stage="http_poll",
                task_id="task-1",
                poll=True,
            )
            event = next(item for item in recorder.read_events(trace_id="trace-redact") if item["stage"] == "http_poll")
            payload_path = pathlib.Path(temp_dir) / "photo_debug" / event["data"]["payloads"]["request"]["path"]
            payload = payload_path.read_text(encoding="utf-8")
            self.assertNotIn("header-secret", payload)
            self.assertNotIn("body-secret", payload)
            self.assertIn("***", payload)
            redacted_response = recorder._sanitize('{"token":"response-secret"}')
            self.assertEqual('{"token":"***"}', redacted_response)
            self.assertIn("token=***", event["data"]["endpoint"])

    def test_download_exception_is_recorded_as_http_exception(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(
                    enabled=True,
                    capture_mode="full_with_secrets",
                    include_secrets=True,
                    capture_payloads=True,
                ),
            )
            harness = _DownloadFailureHarness(temp_dir, recorder)
            harness._append_photo_generation_trace_event("trace-download", "request_received")
            fake_aiohttp = types.ModuleType("aiohttp")
            fake_aiohttp.ClientTimeout = lambda **_kwargs: object()
            with mock.patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
                result = asyncio.run(
                    harness._download_external_image_url_once(
                        "https://provider.test/generated.png?token=query-secret",
                        session_key="private:test",
                    )
                )

            self.assertEqual("", result[0])
            event = next(
                item
                for item in recorder.read_events(trace_id="trace-download")
                if item["stage"] == "http_exception"
            )
            self.assertEqual("error", event["status"])
            self.assertEqual("RuntimeError", event["data"]["error"]["type"])
            request_meta = event["data"]["payloads"]["request"]
            request_path = pathlib.Path(temp_dir) / "photo_debug" / request_meta["path"]
            request_payload = request_path.read_text(encoding="utf-8")
            self.assertIn("header-secret", request_payload)
            self.assertIn("query-secret", request_payload)


if __name__ == "__main__":
    unittest.main()
