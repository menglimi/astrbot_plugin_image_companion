from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_adapters import (  # noqa: E402
    ComfyUIServiceAdapter,
    GenerationMetrics,
    endpoint_capabilities,
    endpoint_model_profile,
    path_within_roots,
    redact_sensitive,
    validate_output_image,
)
from generation_engine import ReferencePlan, RouteDefinition, RouteKey  # noqa: E402
from generation_profiles import AnimaPromptCompiler  # noqa: E402
from test_generation_engine import _spec  # noqa: E402


class _Service:
    def __init__(self):
        self.slots = None
        self.mapping = None

    def inspect_workflow(self, workflow_id):
        return {
            "fingerprint": "current-fingerprint",
            "slots": [
                {"name": "positive_prompt"},
                {"name": "negative_prompt"},
                {"name": "identity_image"},
                {"name": "seed"},
                {"name": "image_output"},
            ]
        }

    async def submit_generation(self, workflow_id, slots, *, mapping=None):
        self.slots = slots
        self.mapping = mapping
        return {"task_id": "task-1"}

    async def get_result(self, task_id):
        return {"status": "completed", "outputs": [{"kind": "images", "url": "http://local/result.png"}]}

    async def cancel(self, task_id):
        return {"status": "cancel_requested"}


class EndpointProfileTests(unittest.TestCase):
    def test_capability_override_beats_model_inference(self):
        endpoint = {
            "platform": "proxy",
            "model": "unknown",
            "capabilities": {
                "edit": True,
                "negative_prompt": True,
                "max_reference_images": 3,
                "reference_roles": ["identity", "outfit", "style"],
            },
        }
        value = endpoint_capabilities(endpoint)
        self.assertEqual("endpoint_override", value.source)
        self.assertEqual(3, value.max_reference_images)
        self.assertTrue(value.negative_prompt)

    def test_endpoint_model_profiles_are_isolated(self):
        self.assertEqual("nai", endpoint_model_profile({"platform": "novelai"}))
        self.assertEqual("generic_tags", endpoint_model_profile({"tag_prompt": True}))
        self.assertEqual("generic_natural", endpoint_model_profile({"platform": "openai"}))

    def test_redaction_and_metrics(self):
        redacted = redact_sensitive({"api_key": "secret", "url": "https://x.test/a?token=secret"})
        self.assertEqual("***", redacted["api_key"])
        self.assertNotIn("secret", redacted["url"])
        metrics = GenerationMetrics()
        metrics.record(route="anima", ok=False, elapsed_ms=30, error_code="backend_timeout")
        self.assertEqual(1, metrics.snapshot()["counters"]["error:backend_timeout"])


class ComfyUIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_anima_prompt_and_reference_use_named_slots(self):
        service = _Service()
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "identity.png"
            reference.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            spec = _spec()
            binding = spec.references[0]
            binding = type(binding)(
                reference_id=binding.reference_id,
                path=str(reference),
                roles=binding.roles,
                priority=binding.priority,
            )
            plan = ReferencePlan((binding,), (), (), True)
            adapter = ComfyUIServiceAdapter(
                service,
                allowed_reference_roots=(directory,),
                materialize=lambda url, request_id: asyncio.sleep(0, result="/tmp/result.png"),
                poll_interval=0.01,
            )
            route = RouteDefinition("anima", RouteKey("comfyui", "anima", "selfie", "anima.json"), timeout_seconds=2)
            prompt = AnimaPromptCompiler().compile(spec)
            result = await adapter.generate(route, spec, prompt, plan, [])
            self.assertTrue(result.ok)
            self.assertIn("summer pajamas", service.slots["positive_prompt"])
            self.assertIn("wool sweater", service.slots["negative_prompt"])
            self.assertIn("identity_image", service.slots)
            self.assertEqual(
                ["workflow_mapping", "submission", "result"],
                [item["stage"] for item in result.trace],
            )

    async def test_confirmed_route_mapping_is_forwarded_to_public_service(self):
        service = _Service()
        spec = _spec()
        adapter = ComfyUIServiceAdapter(service, poll_interval=0.01)
        mapping = {"positive_prompt": {"node_id": "41", "input_name": "text"}}
        route = RouteDefinition(
            "mapped",
            RouteKey("comfyui", "anima", "selfie", "custom.json"),
            timeout_seconds=2,
            settings={"mapping": mapping},
        )
        result = await adapter.generate(
            route,
            spec,
            AnimaPromptCompiler().compile(spec),
            ReferencePlan((), spec.references, ("references:0/1",), True),
            [],
        )
        self.assertTrue(result.ok)
        self.assertEqual(mapping, service.mapping)

    async def test_changed_workflow_fingerprint_rejects_confirmed_mapping(self):
        service = _Service()
        spec = _spec()
        adapter = ComfyUIServiceAdapter(service, poll_interval=0.01)
        route = RouteDefinition(
            "stale",
            RouteKey("comfyui", "anima", "selfie", "changed.json"),
            settings={
                "mapping": {"positive_prompt": {"node_id": "1", "input_name": "text"}},
                "workflow_fingerprint": "previous-fingerprint",
            },
        )
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            await adapter.generate(
                route,
                spec,
                AnimaPromptCompiler().compile(spec),
                ReferencePlan((), (), (), True),
                [],
            )

    async def test_path_boundary_and_output_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            self.assertTrue(path_within_roots(str(image), (directory,)))
            self.assertEqual((True, "ok"), validate_output_image(str(image)))
            outside = Path("/tmp") / "not-inside-this-root.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            try:
                self.assertFalse(path_within_roots(str(outside), (directory,)))
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
