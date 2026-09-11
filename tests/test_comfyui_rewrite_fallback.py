import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_image_companion.comfyui_prompts import original_prompt_slots
from astrbot_plugin_image_companion.comfyui_service import ComfyUIService
from astrbot_plugin_image_companion.comfyui_workflows import WorkflowError, fill_workflow, infer_mapping, validate_mapping
from astrbot_plugin_image_companion.main import ImageCompanionPlugin, ImageCompanionExtensionAPI
from test_native_comfyui import graph


def anima_graph():
    workflow = graph()
    workflow["6"] = {"class_type": "Simple String", "inputs": {"text": ""}}
    workflow["7"] = {"class_type": "AstrBot Prompt Router", "inputs": {"prompt": ["6", 0]}}
    workflow["1"] = {"class_type": "AnimaPromptPlusClipEncode", "inputs": {
        "quality_prompt": "fixed quality", "clothing_tags": ["7", 0], "pose_tags": ["7", 1],
        "background_tags": ["7", 2], "extra_prompt": ["7", 3],
    }}
    return workflow


def test_anima_fallback_preserves_original_router_and_authoritative_slots():
    workflow = anima_graph()
    before = copy.deepcopy(workflow)
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    values = original_prompt_slots(workflow, mapping, {
        "prompt_text": "portrait at the window, sitting, blue pajamas", "negative_prompt": "heavy coat",
        "semantic_prompt_slots": {"clothing_prompt": "blue pajamas", "unmapped_seed": "999"},
    })
    result = fill_workflow(workflow, mapping, values)
    assert result["6"]["inputs"]["text"] == "portrait at the window, sitting, blue pajamas"
    assert result["1"]["inputs"]["clothing_tags"] == "blue pajamas"
    assert result["1"]["inputs"]["pose_tags"] == ["7", 1]
    assert result["1"]["inputs"]["background_tags"] == ["7", 2]
    assert result["2"]["inputs"]["text"] == "bad anatomy, heavy coat"
    assert result["4"]["inputs"]["seed"] == 42
    assert workflow == before


def test_anima_without_generic_input_uses_known_extra_slot():
    workflow = anima_graph()
    for field in ("clothing_tags", "pose_tags", "background_tags", "extra_prompt"):
        workflow["1"]["inputs"][field] = ""
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    assert "positive_prompt" not in mapping["fields"]
    slots = original_prompt_slots(workflow, mapping, {"prompt_text": "portrait near a window"})
    assert slots["extra_prompt"] == "portrait near a window"


def test_incomplete_unknown_multi_input_workflow_cannot_silently_drop_content():
    workflow = {"1": {"class_type": "CustomEncoder", "inputs": {"person": "", "scene": ""}}}
    mapping = {"fields": {name: {"node_id": "1", "input_name": field, "kind": "prompt", "mode": "replace"}
                          for name, field in (("positive_prompt", "person"), ("scene_detail", "scene"))}}
    with pytest.raises(WorkflowError, match="必要槽位：scene_detail"):
        original_prompt_slots(workflow, mapping, {"prompt_text": "portrait at sea"})
    values = original_prompt_slots(workflow, mapping, {"prompt_text": "portrait at sea", "semantic_prompt_slots": {"scene_detail": "beach"}})
    assert values["scene_detail"] == "beach"


def test_missing_negative_input_cannot_discard_existing_constraints():
    workflow = graph()
    mapping = validate_mapping(workflow, infer_mapping(workflow))
    del mapping["fields"]["negative_prompt"]
    with pytest.raises(WorkflowError, match="负面词入口"):
        original_prompt_slots(workflow, mapping, {"prompt_text": "portrait", "negative_prompt": "coat"})


@pytest.fixture
def service(tmp_path):
    instance = ComfyUIService({"base_url": "http://test.invalid", "random_seed": False,
                              "workflows": [{"name": "test", "workflow": graph()}]}, tmp_path)
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/prompt":
            return {"prompt_id": "one-task"}
        if path == "/queue":
            return {}
        return {"one-task": {"status": {"status_str": "success", "completed": True},
                             "outputs": {"5": {"images": [{"filename": "final.png"}]}}}}
    instance._request = request
    instance.download = AsyncMock(return_value="/archive.png")
    return instance, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "invalid_json", "unknown_slot", "missing_provider"])
async def test_rewrite_failure_falls_back_before_one_submission(service, failure):
    instance, calls = service
    model_calls = []
    async def model(prompt):
        model_calls.append(prompt)
        if failure == "timeout":
            raise TimeoutError()
        if failure in {"rate_limit", "missing_provider"}:
            raise WorkflowError("model unavailable")
        if failure == "invalid_json":
            return "not JSON"
        return '{"slots":{"seed":999},"orientation":"portrait"}'
    result = await instance.generate_image("test", {"prompt_text": "two people at sea", "negative_prompt": "heavy coat", "request_text": "横图"}, [], model)
    submitted = [kwargs["json"]["prompt"] for method, path, kwargs in calls if path == "/prompt"]
    assert len(model_calls) == len(submitted) == 1
    assert result["rewrite_fallback"] is True and result["dimensions"] == (1536, 1024)
    assert submitted[0]["1"]["inputs"]["text"] == "fixed positive, two people at sea"
    assert submitted[0]["2"]["inputs"]["text"] == "bad anatomy, heavy coat"
    assert submitted[0]["4"]["inputs"]["seed"] == 42


@pytest.mark.asyncio
async def test_success_and_manual_mode_do_not_report_degradation(service):
    instance, _ = service
    async def model(prompt):
        return json.dumps({"slots": {"positive_prompt": "rewritten", "negative_prompt": ""}, "orientation": "square"})
    result = await instance.generate_image("test", {"prompt_text": "original"}, [], model)
    assert result["rewrite_fallback"] is False and result["dimensions"] == (1024, 1024)
    result = await instance.generate_image("test", {"prompt_text": "original"}, [])
    assert result["rewrite_fallback"] is False


@pytest.mark.asyncio
async def test_cancellation_and_invalid_mapping_never_fall_back(service):
    instance, calls = service
    async def cancelled(prompt):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await instance.generate_image("test", {"prompt_text": "portrait"}, [], cancelled)
    assert not calls
    model = AsyncMock(return_value="not JSON")
    with pytest.raises(WorkflowError):
        await instance.generate_image("test", {"prompt_text": "portrait"}, [], model, mapping_override={"unknown_slot": {}})
    model.assert_not_awaited()
    assert not calls


@pytest.mark.asyncio
async def test_backend_failure_does_not_repeat_generation_after_fallback(service):
    instance, calls = service
    async def failed_result(task):
        return {"status": "failed", "error": "ComfyUI execution failed", "outputs": []}
    instance.get_result = failed_result
    with pytest.raises(WorkflowError, match="execution failed"):
        await instance.generate_image("test", {"prompt_text": "portrait"}, [], AsyncMock(side_effect=TimeoutError()))
    assert sum(path == "/prompt" for _, path, _ in calls) == 1
    assert sum(path == "/queue" for _, path, _ in calls) == 1


@pytest.mark.asyncio
async def test_unconfigured_provider_fails_inside_callback_for_generation_fallback(service):
    instance, _ = service
    plugin = ImageCompanionPlugin.__new__(ImageCompanionPlugin)
    plugin.config = {"comfyui": {}}
    plugin.context = SimpleNamespace(llm_generate=AsyncMock())
    result = await instance.generate_image("test", {"prompt_text": "portrait"}, [], plugin.comfyui_model_call())
    assert result["rewrite_fallback"]
    plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_formal_image_task_reports_success_with_degraded_capability(tmp_path):
    image = tmp_path / "output.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    api = ImageCompanionExtensionAPI.__new__(ImageCompanionExtensionAPI)
    api._reference_leases = {}
    api.generate_for_companion = AsyncMock(return_value={
        "image_path": str(image), "backend": "comfyui",
        "metadata": {"task_id": "actual-task", "rewrite_fallback": True},
    })
    result = await api.execute_task({"input": {}})
    assert result["status"] == "succeeded"
    assert result["backend_task_id"] == "actual-task"
    assert result["degraded_capabilities"] == ["prompt_rewrite:original"]


@pytest.mark.asyncio
async def test_formal_unified_task_preserves_backend_and_degraded_capabilities(tmp_path):
    image = tmp_path / "output.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    api = ImageCompanionExtensionAPI.__new__(ImageCompanionExtensionAPI)
    api._reference_leases = {}
    api.generate_for_companion = AsyncMock(return_value={
        "image_path": str(image), "backend": "统一引擎/comfyui/default",
        "metadata": {
            "task_id": "unified-task",
            "degraded_capabilities": ["prompt_rewrite:original", "reference:missing"],
        },
    })
    result = await api.execute_task({"input": {}})
    assert result["backend"] == "comfyui"
    assert result["backend_task_id"] == "unified-task"
    assert result["degraded_capabilities"] == ["prompt_rewrite:original", "reference:missing"]
