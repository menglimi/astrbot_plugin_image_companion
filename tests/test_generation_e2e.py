from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
COMFY_CANDIDATES = (
    ROOT.parent / "comfyui-plugin",
    ROOT.parent / "astrbot_plugin_comfyui",
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from generation_adapters import ComfyUIServiceAdapter  # noqa: E402
from generation_engine import GenerationEngine, RouteDefinition, RouteKey, RouteRegistry, route_cache_key  # noqa: E402
from generation_profiles import default_model_profile_registry  # noqa: E402
from test_generation_engine import _spec  # noqa: E402

ComfyUIPublicService = None
for candidate in COMFY_CANDIDATES:
    if not (candidate / "public_service.py").is_file():
        continue
    sys.path.insert(0, str(candidate))
    try:
        from public_service import ComfyUIPublicService  # type: ignore[assignment]  # noqa: E402
    except ImportError:
        continue
    break


class _Transport:
    def __init__(self):
        self.prompt = None

    async def request(self, method, path, body):
        if path == "/prompt":
            self.prompt = body["prompt"]
            return {"prompt_id": "e2e-task"}
        if path == "/history/e2e-task":
            return {"e2e-task": {"outputs": {"5": {"images": [{"filename": "result.png", "type": "output"}]}}}}
        if path == "/queue":
            return {"queue_running": [], "queue_pending": []}
        return {}


class UnifiedGenerationEndToEndTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(ComfyUIPublicService is None, "ComfyUI public service checkout is unavailable")
    async def test_companion_spec_to_anima_workflow_named_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_name = "Anima+文本2+图片1.json"
            (root / workflow_name).write_text(json.dumps({
                "1": {"class_type": "Simple String", "_meta": {"title": "Positive Prompt"}, "inputs": {"text": ""}},
                "2": {"class_type": "Simple String", "_meta": {"title": "Negative Prompt"}, "inputs": {"text": ""}},
                "3": {"class_type": "ETN_LoadImageBase64", "_meta": {"title": "Identity Reference"}, "inputs": {"image": ""}},
                "4": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
                "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
            }), encoding="utf-8")
            identity = root / "identity.png"
            identity.write_bytes(b"\x89PNG\r\n\x1a\nidentity")
            transport = _Transport()
            service = ComfyUIPublicService(
                workflows_dir=root,
                server_ip="127.0.0.1:8188",
                client_id="e2e",
                transport=transport,
            )
            adapter = ComfyUIServiceAdapter(
                service,
                allowed_reference_roots=(directory,),
                materialize=lambda url, request_id: __import__("asyncio").sleep(0, result=str(root / "result.png")),
                poll_interval=0.01,
            )
            spec = _spec(request_id="e2e")
            spec = replace(
                spec,
                references=(replace(spec.references[0], path=str(identity)),),
            )
            routes = RouteRegistry()
            routes.register(RouteDefinition("anima", RouteKey("comfyui", "anima", "selfie", workflow_name), timeout_seconds=2))
            engine = GenerationEngine(default_model_profile_registry(), routes, {"comfyui": adapter})
            result = await engine.generate(spec, "anima")
            self.assertTrue(result.ok)
            self.assertIn("summer pajamas", transport.prompt["1"]["inputs"]["text"])
            self.assertIn("wool sweater", transport.prompt["2"]["inputs"]["text"])
            self.assertTrue(transport.prompt["3"]["inputs"]["image"])

    async def test_route_cache_key_performance(self):
        spec = _spec()
        route = RouteDefinition("anima", RouteKey("comfyui", "anima", "selfie", "wf"))
        started = time.perf_counter()
        values = {route_cache_key(replace(spec, request_id=str(index)), route) for index in range(5000)}
        elapsed = time.perf_counter() - started
        # request_id intentionally does not change semantic cache identity.
        self.assertEqual(1, len(values))
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
