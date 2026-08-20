from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_debug import GenerationDebugConfig, GenerationDebugRecorder, redact  # noqa: E402


class GenerationDebugConfigTests(unittest.TestCase):
    def test_invalid_values_are_normalized(self) -> None:
        config = GenerationDebugConfig.from_mapping(
            {
                "enabled": True,
                "capture_mode": "unknown",
                "retention_days": "-2",
                "backup_count": "999",
                "max_body_kb": "0",
            }
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.capture_mode, "redacted")
        self.assertEqual(config.retention_days, 0)
        self.assertEqual(config.backup_count, 100)
        self.assertEqual(config.max_body_kb, 1)

    def test_string_false_does_not_enable_secret_capture(self) -> None:
        config = GenerationDebugConfig.from_mapping(
            {"enabled": "false", "include_secrets": "false", "capture_payloads": "false"}
        )
        self.assertFalse(config.enabled)
        self.assertFalse(config.include_secrets)
        self.assertFalse(config.capture_payloads)


class GenerationDebugRecorderTests(unittest.TestCase):
    def make_recorder(self, **config):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        options = {"enabled": True, "max_file_size_kb": 64, "backup_count": 2}
        options.update(config)
        recorder = GenerationDebugRecorder(
            temp_dir.name,
            GenerationDebugConfig(**options),
        )
        return recorder, Path(temp_dir.name)

    def test_off_mode_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(enabled=True, capture_mode="off"),
            )
            self.assertIsNone(recorder.emit("trace", "request_received"))
            self.assertFalse((Path(temp_dir) / "photo_debug").exists())

    def test_envelope_sequence_context_and_terminal_manifest(self) -> None:
        recorder, root = self.make_recorder(capture_mode="full")
        trace_id = recorder.start_trace(
            "trace-1",
            request_id="request-1",
            context={"session_key": "private:123", "prompt": "a full prompt"},
        )
        first = recorder.emit(
            trace_id,
            "payload_built",
            operation="selfie",
            backend="comfyui",
            data={"prompt": "a full prompt", "attempt": 1},
        )
        second = recorder.finish_trace(trace_id, data={"image_path": "C:/output/result.png"})
        self.assertEqual(trace_id, "trace-1")
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["request_id"], "request-1")
        self.assertEqual(second["context"]["session_key"], "private:123")

        events = recorder.read_events(trace_id=trace_id)
        self.assertEqual([item["seq"] for item in events], [1, 2])
        manifest_path = root / "photo_debug" / "traces" / "trace-1" / "manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["event_count"], 2)
        self.assertEqual(manifest["last_status"], "completed")

    def test_redacted_mode_masks_nested_and_inline_credentials(self) -> None:
        recorder, _ = self.make_recorder(capture_mode="redacted")
        recorder.start_trace("trace-redacted")
        event = recorder.emit(
            "trace-redacted",
            "request_received",
            data={
                "api_key": "sk-secret",
                "headers": {"Authorization": "Bearer abc123", "Cookie": "sid=xyz"},
                "url": "https://example.test/generate?token=url-secret&x=1",
                "prompt": "keep this prompt visible",
            },
        )
        payload = event["data"]
        self.assertEqual(payload["api_key"], "***")
        self.assertEqual(payload["headers"]["Authorization"], "***")
        self.assertEqual(payload["headers"]["Cookie"], "***")
        self.assertIn("token=***", payload["url"])
        self.assertEqual(payload["prompt"], "keep this prompt visible")
        self.assertEqual(redact("Authorization: Bearer abc"), "Authorization: ***")

    def test_full_with_secrets_requires_explicit_include_flag(self) -> None:
        recorder, _ = self.make_recorder(capture_mode="full_with_secrets", include_secrets=False)
        recorder.start_trace("trace-gated")
        gated = recorder.emit("trace-gated", "config_snapshot", data={"api_key": "secret"})
        self.assertEqual(gated["data"]["api_key"], "***")

        recorder_with_secrets, _ = self.make_recorder(capture_mode="full_with_secrets", include_secrets=True)
        recorder_with_secrets.start_trace("trace-open")
        open_event = recorder_with_secrets.emit("trace-open", "config_snapshot", data={"api_key": "secret"})
        self.assertEqual(open_event["data"]["api_key"], "secret")

    def test_payload_capture_and_hash_when_disabled(self) -> None:
        recorder, root = self.make_recorder(capture_mode="full", capture_payloads=True, max_body_kb=1)
        recorder.start_trace("trace-payload")
        event = recorder.emit(
            "trace-payload",
            "backend_submitted",
            payloads={"request": {"prompt": "hello", "width": 1024, "api_key": "secret"}},
        )
        metadata = event["data"]["payloads"]["request"]
        self.assertTrue(metadata["captured"])
        self.assertEqual(metadata["mime_type"], "application/json")
        payload_path = root / "photo_debug" / metadata["path"]
        self.assertTrue(payload_path.exists())
        self.assertNotIn("secret", payload_path.read_text(encoding="utf-8"))
        self.assertIn('"api_key": "***"', payload_path.read_text(encoding="utf-8"))
        self.assertEqual(len(metadata["sha256"]), 64)

        no_capture, _ = self.make_recorder(capture_mode="full", capture_payloads=False)
        no_capture.start_trace("trace-preview")
        preview_event = no_capture.emit("trace-preview", "payload_built", payloads={"request": "small"})
        preview_meta = preview_event["data"]["payloads"]["request"]
        self.assertFalse(preview_meta["captured"])
        self.assertEqual(preview_meta["preview"], "small")

    def test_rotation_and_recent_reader(self) -> None:
        recorder, root = self.make_recorder(capture_mode="full", max_file_size_kb=1, backup_count=2)
        recorder.start_trace("trace-rotate")
        for index in range(40):
            recorder.emit("trace-rotate", "backend_poll", data={"index": index, "blob": "x" * 90})
        event_files = list((root / "photo_debug").glob("generation*.jsonl"))
        self.assertTrue((root / "photo_debug" / "generation.jsonl").exists())
        self.assertGreaterEqual(len(event_files), 2)
        recent = recorder.read_recent(3)
        self.assertEqual(len(recent), 3)
        self.assertGreater(recent[0]["seq"], recent[-1]["seq"])


if __name__ == "__main__":
    unittest.main()
