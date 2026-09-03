import json
import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_debug import GenerationDebugConfig, GenerationDebugRecorder  # noqa: E402
from _runtime_loader import RUNTIME  # noqa: E402


class _LegacyTraceHarness:
    """Small seam for the mixin method without booting the AstrBot runtime."""

    def __init__(self, runtime, data_dir: str) -> None:
        self._runtime = runtime
        self.data_dir = data_dir
        self.photo_generation_trace_max_size_kb = 64
        self.photo_generation_trace_backup_count = 2
        self._recorder = None
        self._image_service = types.SimpleNamespace(
            config={
                "debug": {
                    "enabled": True,
                    "capture_mode": "full",
                    "max_file_size_kb": 64,
                }
            },
            data_dir=data_dir,
        )

    def _generation_debug_recorder(self, config):
        if self._recorder is None:
            self._recorder = GenerationDebugRecorder(
                self.data_dir,
                GenerationDebugConfig.from_mapping(config.get("debug", {})),
            )
        return self._recorder

    def _photo_generation_trace_max_bytes(self):
        return self.photo_generation_trace_max_size_kb * 1024

    def _photo_generation_trace_backup_count(self):
        return self.photo_generation_trace_backup_count

    def _photo_generation_trace_file_path(self):
        return pathlib.Path(self.data_dir) / "photo_generation_trace.txt"

    def _rotate_photo_generation_trace_files(self, path):
        for index in range(self.photo_generation_trace_backup_count, 0, -1):
            source = path if index == 1 else path.with_name(
                f"{path.stem}.{index - 1}{path.suffix}"
            )
            target = path.with_name(f"{path.stem}.{index}{path.suffix}")
            if source.exists():
                source.replace(target)

    @staticmethod
    def _sanitize_photo_generation_trace_value(value, **_kwargs):
        return value


class LegacyTraceRecorderBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = RUNTIME

    def test_legacy_trace_is_dual_written_with_contiguous_sequences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            harness = _LegacyTraceHarness(self.runtime, temp_dir)
            method = self.runtime.ProactiveMessageMixin._append_photo_generation_trace_event

            method(
                harness,
                "trace-bridge",
                "request_received",
                context={"session": "session-1"},
            )
            method(
                harness,
                "trace-bridge",
                "completed",
                data={"image_path": "C:/output/result.png"},
            )

            legacy_path = pathlib.Path(temp_dir) / "photo_generation_trace.txt"
            legacy_events = [
                json.loads(line)
                for line in legacy_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([event["seq"] for event in legacy_events], [1, 2])

            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(enabled=True, capture_mode="full"),
            )
            debug_events = recorder.read_events(trace_id="trace-bridge")
            self.assertEqual([event["seq"] for event in debug_events], [1, 2])
            self.assertEqual(
                [event["stage"] for event in debug_events],
                ["request_received", "completed"],
            )

    def test_prompt_section_field_accepts_legacy_mapping(self):
        section = {
            "name": "legacy_request",
            "source": "user_request",
            "positive": "a portrait",
            "negative": "blurry",
        }

        self.assertEqual(
            RUNTIME._photo_prompt_section_field(section, "source"),
            "user_request",
        )
        self.assertEqual(
            RUNTIME._photo_prompt_section_field(section, "positive"),
            "a portrait",
        )
        self.assertFalse(
            RUNTIME._photo_prompt_section_field(section, "protected", False)
        )


if __name__ == "__main__":
    unittest.main()
