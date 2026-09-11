from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from astrbot_plugin_image_companion.comfyui_workflows import (
    WorkflowError, WorkflowStore, api_workflow, fill_workflow, fingerprint, infer_mapping, validate_mapping,
)
from astrbot_plugin_image_companion.comfyui_prompts import choose_dimensions, prompt_fields, rewrite_prompts
from astrbot_plugin_image_companion.comfyui_service import ComfyUIService
from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime
from astrbot_plugin_image_companion.main import ImageCompanionPlugin, ImageCompanionExtensionAPI


def graph():
    return {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "fixed positive"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad anatomy"}},
        "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1536, "batch_size": 1}},
        "4": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "latent_image": ["3", 0], "seed": 42, "steps": 20}},
        "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": "fixed"}},
    }


def test_mapping_follows_sampler_roles_and_preserves_graph():
    workflow = graph()
    original = copy.deepcopy(workflow)
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    prepared = fill_workflow(workflow, mapping, {"positive_prompt": "two people at sea", "negative_prompt": "text overlay", "width": 1536, "height": 1024})
    assert prepared["1"]["inputs"]["text"] == "fixed positive, two people at sea"
    assert prepared["2"]["inputs"]["text"] == "bad anatomy, text overlay"
    assert prepared["4"]["inputs"]["positive"] == ["1", 0]
    assert prepared["4"]["inputs"]["steps"] == 20
    assert workflow == original


def test_mapping_refuses_ambiguous_output_and_non_api_graphs():
    workflow = graph()
    workflow["6"] = copy.deepcopy(workflow["5"])
    with pytest.raises(WorkflowError, match="输出"):
        validate_mapping(workflow, infer_mapping(workflow))
    with pytest.raises(WorkflowError, match="API"):
        api_workflow({"nodes": []})
    workflow["4"]["inputs"]["positive"] = ["missing", 0]
    with pytest.raises(WorkflowError, match="连线"):
        api_workflow(workflow)


def test_saved_mapping_rejects_changed_workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    name = store.import_workflow("portrait", graph())
    workflow = store.load(name)
    store.save_mapping(name, infer_mapping(workflow), fingerprint(workflow))
    assert store.mapping(name, workflow)["output_node"] == "5"
    workflow["1"]["inputs"]["text"] = "changed"
    with pytest.raises(WorkflowError, match="变化"):
        store.mapping(name, workflow)
    with pytest.raises(WorkflowError, match="路径"):
        store.load("../secret.json")


def test_numeric_mapping_cannot_change_sampling_parameters():
    workflow = graph()
    mapping = infer_mapping(workflow)
    mapping["fields"]["steps"] = {"node_id": "4", "input_name": "steps", "kind": "number"}
    with pytest.raises(WorkflowError, match="原值"):
        validate_mapping(workflow, mapping)
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    with pytest.raises(WorkflowError, match="有限数字"):
        fill_workflow(workflow, mapping, {"width": float("nan")})


@pytest.mark.parametrize("request_data,orientation,expected", [
    ({"request_text": "双人横图"}, "portrait", (1536, 1024)),
    ({"image_size": "768x1024", "request_text": "横图"}, "landscape", (768, 1024)),
    ({"request_text": "合影"}, "landscape", (1536, 1024)),
    ({"request_text": "修改这张图", "has_reference": True}, "square", None),
])
def test_dimensions_respect_explicit_request_and_reference_geometry(request_data, orientation, expected):
    assert choose_dimensions({}, request_data, orientation) == expected


def test_dimensions_reject_invalid_budget_and_alignment():
    for size in ("1001x1001", "8192x8192", "banana"):
        with pytest.raises(WorkflowError):
            choose_dimensions({}, {"image_size": size}, "")


@pytest.mark.asyncio
async def test_rewriter_uses_rules_and_rejects_hallucinated_slots():
    workflow = graph()
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    calls = []
    async def model(prompt):
        calls.append(prompt)
        return json.dumps({"slots": {"positive_prompt": "two people", "negative_prompt": "extra hands"}, "orientation": "landscape"})
    slots, aspect = await rewrite_prompts(workflow, mapping, {"request_text": "双人合影", "api_key": "never-send"}, model)
    assert aspect == "landscape" and slots["positive_prompt"] == "two people"
    assert len(calls) == 1 and "never-send" not in calls[0] and "bad anatomy" in calls[0]
    assert "<WORKFLOW_DATA>" in calls[0] and "</WORKFLOW_DATA>" in calls[0]
    async def invalid(prompt):
        return '{"slots":{"made_up":"oops"}}'
    with pytest.raises(WorkflowError, match="不一致"):
        await rewrite_prompts(workflow, mapping, {}, invalid)


@pytest.mark.asyncio
async def test_anima_semantics_do_not_duplicate_upstream_prompt():
    workflow = graph()
    workflow["6"] = {"class_type": "Simple String", "inputs": {"text": ""}}
    workflow["7"] = {"class_type": "AstrBot Prompt Router", "inputs": {"prompt": ["6", 0]}}
    workflow["1"] = {"class_type": "AnimaPromptPlusClipEncode", "inputs": {"quality_prompt": "fixed style", "clothing_tags": ["7", 0], "pose_tags": ["7", 1], "background_tags": ["7", 2], "extra_prompt": ["7", 3]}}
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    fields = prompt_fields(mapping, workflow)
    assert "positive_prompt" not in fields
    async def model(prompt):
        return json.dumps({"slots": {name: name for name in fields}, "orientation": "portrait"})
    slots, _ = await rewrite_prompts(workflow, mapping, {"request_text": "窗边睡衣"}, model)
    prepared = fill_workflow(workflow, mapping, slots)
    assert prepared["1"]["inputs"]["clothing_tags"] == "clothing_prompt"
    assert prepared["1"]["inputs"]["quality_prompt"] == "fixed style"
    assert prepared["6"]["inputs"]["text"] == ""


@pytest.mark.asyncio
async def test_http_generation_submits_once_and_selects_final_image(tmp_path):
    requests = []
    async def submit(request):
        requests.append(await request.json())
        return web.json_response({"prompt_id": "task-1"})
    async def history(request):
        return web.json_response({"task-1": {"status": {"status_str": "success"}, "outputs": {
            "preview": {"images": [{"filename": "wrong.png"}]},
            "5": {"images": [{"filename": "final.png", "subfolder": "中文", "type": "output"}]},
        }}})
    async def view(request):
        assert request.query["filename"] == "final.png" and request.query["subfolder"] == "中文"
        return web.Response(body=b"\x89PNG\r\n\x1a\nfixture", content_type="image/png")
    app = web.Application()
    app.router.add_post("/prompt", submit)
    app.router.add_get("/history/{task_id}", history)
    app.router.add_get("/view", view)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    service = ComfyUIService({"base_url": f"http://127.0.0.1:{port}", "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    async def model(prompt):
        return '{"slots":{"positive_prompt":"two friends by the sea","negative_prompt":"extra limbs"},"orientation":"landscape"}'
    try:
        result = await service.generate_image("test", {"request_text": "横图合影"}, [], model)
        assert Path(result["image_path"]).exists()
        assert Path(result["image_path"]).parent.name == "generated_photos"
        assert result["task_id"] == "task-1" and result["dimensions"] == (1536, 1024)
        assert len(requests) == 1
        submitted = requests[0]["prompt"]
        assert submitted["3"]["inputs"]["width"] == 1536
        assert submitted["2"]["inputs"]["text"] == "bad anatomy, extra limbs"
        assert submitted["4"]["inputs"]["seed"] != 42
        assert submitted["4"]["inputs"]["steps"] == 20
    finally:
        await service.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_runtime_unpacks_size_and_keeps_scene():
    class Runtime(ImageGenerationRuntime):
        async def _generate_photo_image_legacy(self, *, workflow_kind, prompt_text, image_size=""):
            assert image_size == "1536x1024"
            assert self._comfyui_task_context["scene"] == {"location": "beach"}
            return "ComfyUI", "", "ok"
    runtime = Runtime.__new__(Runtime)
    runtime._image_service = SimpleNamespace()
    result = await runtime.generate({"workflow_kind": "text2img", "prompt_text": "beach", "limits": {"image_size": "1536x1024"}, "scene": {"location": "beach"}})
    assert result == ("ComfyUI", "", "ok")


@pytest.mark.asyncio
async def test_legacy_runtime_uses_native_without_external_plugin():
    class Service:
        config = {"rewrite_enabled": False}
        async def generate_image(self, name, request, paths, call):
            assert name == "test" and request["semantic_prompt_slots"]["clothing_prompt"] == "blue shirt"
            assert request["image_size"] == "1536x1024"
            return {"image_path": "/output.png", "task_id": "real-task"}
    runtime = ImageGenerationRuntime.__new__(ImageGenerationRuntime)
    runtime._image_service = SimpleNamespace(native_comfyui_service=lambda: Service())
    runtime._image_owner = SimpleNamespace()
    runtime._comfyui_task_context = {"image_size": "1536x1024"}
    runtime._get_comfyui_module = lambda: pytest.fail("external plugin accessed")
    path, note = await runtime._run_comfyui_photo_workflow("test", "prompt", "session", semantic_prompt_slots={"clothing_prompt": "blue shirt"})
    assert path == "/output.png" and "real-task" in note


@pytest.mark.asyncio
async def test_selected_provider_used_for_rewriting():
    calls = []
    async def llm_generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text='{"slots":{}}')
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.config = {"comfyui": {"prompt_provider_id": "dedicated"}}
    plugin.context = SimpleNamespace(llm_generate=llm_generate)
    text = await plugin.comfyui_model_call(SimpleNamespace(photo_prompt_provider_id="chat-model"))("rewrite")
    assert text == '{"slots":{}}' and calls[0]["chat_provider_id"] == "dedicated"
    assert calls[0]["contexts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 401, 500])
async def test_model_provider_failures_have_actionable_safe_messages(status):
    class ProviderFailure(Exception):
        status_code = status
    calls = []
    async def llm_generate(**kwargs):
        calls.append(kwargs)
        raise ProviderFailure("private server detail and api_key=secret")
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.config = {"comfyui": {"prompt_provider_id": "rewrite-model"}}
    plugin.context = SimpleNamespace(llm_generate=llm_generate)
    with pytest.raises(WorkflowError) as caught:
        await plugin.comfyui_model_call()("test")
    assert str(status) in str(caught.value)
    assert "尚未" in str(caught.value) and "secret" not in str(caught.value)
    assert len(calls) == 1
    if status == 429:
        assert ImageCompanionExtensionAPI._generation_failure_code({"note": str(caught.value)}) == ("prompt_model_rate_limited", "prompt_rewrite")


@pytest.mark.asyncio
async def test_model_timeout_is_not_reported_as_comfyui_connection_failure():
    async def llm_generate(**kwargs):
        raise TimeoutError()
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.config = {"comfyui": {"prompt_provider_id": "rewrite-model", "model_timeout_seconds": 60}}
    plugin.context = SimpleNamespace(llm_generate=llm_generate)
    with pytest.raises(WorkflowError, match="提示词模型.*60 秒.*尚未") as caught:
        await plugin.comfyui_model_call()("test")
    assert ImageCompanionExtensionAPI._generation_failure_code({"note": str(caught.value)}) == ("prompt_model_timeout", "prompt_rewrite")


@pytest.mark.asyncio
async def test_analysis_validates_and_saves_model_mapping(tmp_path):
    workflow = graph()
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": workflow}]}, tmp_path)
    definitions = {}
    for node in workflow.values():
        definitions[node["class_type"]] = {"input": {"required": {
            key: ["STRING" if isinstance(value, str) else "INT"] for key, value in node["inputs"].items()
        }}, "output_node": node["class_type"] == "SaveImage"}
    async def request(method, path, **kwargs):
        assert method == "GET" and path.startswith("/object_info/")
        return definitions
    service._request = request
    calls = []
    async def model(prompt):
        calls.append(prompt)
        mapping = infer_mapping(workflow)
        mapping["fields"]["positive_prompt"]["mode"] = "replace"
        return json.dumps(mapping)
    result = await service.analyze("test", model)
    assert result["saved"] and len(calls) == 1
    assert service.store.mapping("test", workflow)["fields"]["positive_prompt"]["mode"] == "replace"
    assert service.store.load("test") == workflow
    async def unsafe(prompt):
        mapping = infer_mapping(workflow)
        mapping["fields"]["positive_prompt"].update(node_id="5", input_name="filename_prefix")
        return json.dumps(mapping)
    with pytest.raises(WorkflowError, match="文件或连接"):
        await service.analyze("test", unsafe)


@pytest.mark.asyncio
async def test_analysis_omits_long_custom_input_values(tmp_path):
    workflow = graph()
    workflow["8"] = {"class_type": "CustomNode", "inputs": {"payload": "x" * 3000}}
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": workflow}]}, tmp_path)
    definitions = {
        node["class_type"]: {"input": {"required": {key: ["STRING" if isinstance(value, str) else "INT"] for key, value in node["inputs"].items()}}, "output_node": node["class_type"] == "SaveImage"}
        for node in workflow.values()
    }
    async def request(method, path, **kwargs):
        return definitions
    service._request = request
    prompts = []
    async def model(prompt):
        prompts.append(prompt)
        mapping = infer_mapping(graph())
        return json.dumps(mapping)
    await service.analyze("test", model, save=False)
    assert "x" * 3000 not in prompts[0]
    assert "[omitted: long value]" in prompts[0]


@pytest.mark.asyncio
async def test_bad_model_output_without_original_never_submits_and_execution_error_does_not_retry(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/prompt":
            return {"prompt_id": "failed-task"}
        return {"failed-task": {"status": {"status_str": "error", "messages": [["execution_error", {"node_id": "4"}]]}}}
    service._request = request
    async def bad(prompt):
        return "not JSON"
    with pytest.raises(WorkflowError, match="原提示词也无法降级"):
        await service.generate_image("test", {}, [], bad)
    assert calls == []
    with pytest.raises(WorkflowError, match="执行失败"):
        await service.generate_image("test", {"prompt_text": "a beach"}, [])
    assert calls.count(("POST", "/prompt")) == 1
    assert not service._tasks


@pytest.mark.asyncio
async def test_running_history_stays_pending_until_success(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    service._tasks["task"] = "5"
    service._request = lambda method, path, **kwargs: asyncio.sleep(
        0,
        result={"task": {"status": {"status_str": "running"}, "outputs": {}}},
    )
    result = await service.get_result("task")
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_poll_timeout_cancels_target_without_masking_timeout(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/prompt":
            return {"prompt_id": "task"}
        return {}

    async def timed_out(_task_id):
        raise asyncio.TimeoutError()

    service._request = request
    service.get_result = timed_out
    with pytest.raises(WorkflowError, match="超时"):
        await service.generate_image("test", {"prompt_text": "a beach"}, [], timeout_seconds=5)
    assert calls == [("POST", "/prompt"), ("POST", "/queue")]
    assert not service._tasks


@pytest.mark.asyncio
async def test_materialization_error_cancels_target_once_and_preserves_error(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/prompt":
            return {"prompt_id": "task"}
        return {"task": {"status": {"status_str": "success"}, "outputs": {"5": {"images": [{"filename": "result.png"}]}}}}

    async def download(_url):
        raise RuntimeError("disk full")

    service._request = request
    service.download = download
    with pytest.raises(RuntimeError, match="disk full"):
        await service.generate_image("test", {"prompt_text": "a beach"}, [])
    assert calls == [("POST", "/prompt"), ("GET", "/history/task"), ("POST", "/queue")]
    assert not service._tasks


def test_invalid_native_config_degrades_without_breaking_other_backends(tmp_path):
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.config = {"comfyui": {"base_url": "http://user:password@example.invalid"}}
    plugin.data_dir = str(tmp_path)
    plugin._native_comfyui_config = None
    assert plugin.native_comfyui_service() is None
    assert "有效" in plugin._native_comfyui_error


@pytest.mark.asyncio
async def test_standard_reference_upload_uses_returned_name_not_base64(tmp_path):
    workflow = graph()
    workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": "old.png"}}
    workflow["4"]["inputs"]["reference"] = ["6", 0]
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": workflow}]}, tmp_path)
    calls = []
    async def request(method, path, **kwargs):
        calls.append(path)
        if path == "/upload/image":
            assert "data" in kwargs and "json" not in kwargs
            return {"name": "uploaded.png", "subfolder": "references"}
        if path == "/prompt":
            assert kwargs["json"]["prompt"]["6"]["inputs"]["image"] == "references/uploaded.png"
            return {"prompt_id": "task"}
        return {"task": {"status": {"status_str": "success"}, "outputs": {"5": {"images": [{"filename": "result.png"}]}}}}
    async def download(url):
        return "/saved.png"
    service._request, service.download = request, download
    result = await service.generate_image("test", {"prompt_text": "edit"}, [str(reference)])
    assert result["image_path"] == "/saved.png"
    assert calls[:2] == ["/upload/image", "/prompt"]


@pytest.mark.asyncio
async def test_unified_adapter_uses_native_rewriter_and_route_mapping():
    from generation_adapters import ComfyUIServiceAdapter
    from generation_engine import ReferencePlan, RouteDefinition, RouteKey
    from generation_profiles import AnimaPromptCompiler
    from test_generation_engine import _spec
    calls = []
    class Service:
        def inspect_workflow(self, name):
            return {"fingerprint": "confirmed"}
        async def generate_image(self, name, request, references, call, **kwargs):
            calls.append((request, kwargs, call))
            return {"image_path": "/saved.png", "task_id": "native-task", "dimensions": (1536, 1024)}
    async def model(prompt):
        return "{}"
    spec = _spec()
    route = RouteDefinition("native", RouteKey("comfyui", "anima", "selfie", "test"), timeout_seconds=50,
                            settings={"workflow_fingerprint": "confirmed", "mapping": {"positive_prompt": {"node_id": "1", "input_name": "text"}}})
    adapter = ComfyUIServiceAdapter(Service(), rewrite_call=model, native_request={"image_size": "1536x1024"})
    result = await adapter.generate(route, spec, AnimaPromptCompiler().compile(spec), ReferencePlan((), (), (), True), [])
    assert result.task_id == "native-task" and result.ok
    assert calls[0][0]["image_size"] == "1536x1024" and calls[0][2] is model
    assert calls[0][1]["timeout_seconds"] == 50 and calls[0][1]["mapping_override"] == route.settings["mapping"]


@pytest.mark.asyncio
async def test_native_generic_reference_input_accepts_identity_role(tmp_path):
    from generation_adapters import ComfyUIServiceAdapter
    from generation_engine import ReferencePlanner, RouteDefinition, RouteKey
    from generation_contracts import ReferenceBindingV1
    workflow = graph()
    workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": "fixed.png"}}
    workflow["4"]["inputs"]["reference"] = ["6", 0]
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "test", "workflow": workflow}]}, tmp_path)
    capabilities = await ComfyUIServiceAdapter(service).capabilities(RouteDefinition("native", RouteKey("comfyui", "anima", "selfie", "test")))
    reference = ReferenceBindingV1("identity", "/identity.png", roles=("identity",))
    assert capabilities.max_reference_images == 1
    assert ReferencePlanner().plan((reference,), capabilities).submitted == (reference,)


@pytest.mark.asyncio
async def test_cancellation_is_targeted_and_never_global(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid"}, tmp_path)
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}
    service._request = request
    with pytest.raises(WorkflowError, match="本插件"):
        await service.cancel("someone-else")
    assert not calls
    service._tasks["ours"] = "5"
    await service.cancel("ours")
    assert calls == [("POST", "/queue", {"json": {"delete": ["ours"]}})]


def test_configured_file_alias_retains_manual_mapping(tmp_path):
    path = tmp_path / "selfie+文本1+图片0.json"
    path.write_text(json.dumps(graph()), encoding="utf-8")
    mapping = infer_mapping(graph())
    mapping["fields"]["positive_prompt"]["mode"] = "preserve"
    store = WorkflowStore(tmp_path / "workflows", [{"name": path.name, "path": str(path), "mapping": mapping}])
    assert store.mapping("selfie", store.load("selfie"))["fields"]["positive_prompt"]["mode"] == "preserve"
