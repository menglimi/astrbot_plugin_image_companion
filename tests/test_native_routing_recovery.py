from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_image_companion.comfyui_config import normalize_config, public_config
from astrbot_plugin_image_companion.comfyui_prompts import choose_dimensions
from astrbot_plugin_image_companion.comfyui_service import ComfyUIService
from astrbot_plugin_image_companion.comfyui_workflows import WorkflowError
from astrbot_plugin_image_companion.image_runtime import ImageGenerationRuntime
from astrbot_plugin_image_companion.main import ImageCompanionExtensionAPI
from test_native_comfyui import graph
from test_migrated_photo_prompt_reliability import _PhotoReliabilityHarness


def image_graph():
    workflow = graph()
    workflow["3"] = {"class_type": "LoadImage", "inputs": {"image": "example.png"}}
    return workflow


@pytest.mark.parametrize("kind", ["selfie", "portrait", "text2img"])
def test_workflow_selection_uses_actual_reference_presence(kind):
    runtime = ImageGenerationRuntime.__new__(ImageGenerationRuntime)
    runtime._image_service = SimpleNamespace()
    runtime._image_owner = SimpleNamespace()
    runtime.comfyui_text2img_workflow_name = "t2i"
    runtime.comfyui_selfie_workflow_name = "i2i"
    assert runtime._choose_photo_workflow_name(kind, has_reference=False) == "t2i"
    assert runtime._choose_photo_workflow_name("selfie", has_reference=True) == "i2i"


@pytest.mark.asyncio
async def test_reference_workflow_cannot_submit_its_placeholder(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "i2i", "workflow": image_graph()}]}, tmp_path)
    service._request = AsyncMock()
    with pytest.raises(WorkflowError, match="缺少本次参考图") as failure:
        await service.generate_image("i2i", {"prompt_text": "casual portrait"}, [])
    assert not failure.value.submitted
    service._request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,submitted", [(WorkflowError("mapping invalid"), False), (WorkflowError("timeout", submitted=True, task_id="job"), True), (None, False)])
async def test_auto_native_preflight_failure_falls_back_once(tmp_path, failure, submitted):
    runtime = _PhotoReliabilityHarness(tmp_path)
    runtime.photo_generation_backend = "auto"
    runtime.comfyui_text2img_workflow_name = "t2i"
    runtime.comfyui_selfie_workflow_name = "i2i"
    calls = []

    async def generate(name, request, references, call):
        calls.append((name, request, references))
        if failure:
            raise failure
        return {"image_path": str(runtime.generated_path), "task_id": "job"}

    native = SimpleNamespace(config={"rewrite_enabled": False}, generate_image=generate)
    runtime._native_comfyui_service = lambda: native
    runtime._comfyui_photo_available = lambda: True
    runtime._local_photo_generation_busy_state = lambda **kwargs: {}
    result = await runtime._generate_photo_image_legacy(workflow_kind="selfie", prompt_text="casual outfit portrait", session_key="daily_outfit")
    assert calls[0][0] == "t2i" and calls[0][2] == []
    assert len(calls) == 1
    if failure and not submitted:
        assert result[0] == "在线图片 API" and len(runtime.external_calls) == 1
    else:
        assert result[0] == "ComfyUI" and not runtime.external_calls


@pytest.mark.asyncio
async def test_submitted_polling_failure_is_marked_and_not_retried(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "t2i", "workflow": graph()}]}, tmp_path)
    service._request = AsyncMock(return_value={"prompt_id": "job"})
    service.get_result = AsyncMock(side_effect=OSError("lost response"))
    service._cancel_best_effort = AsyncMock()
    with pytest.raises(OSError) as failure:
        await service.generate_image("t2i", {"prompt_text": "portrait"}, [])
    assert failure.value.submitted and failure.value.task_id == "job"
    assert service._request.await_count == 1


@pytest.mark.asyncio
async def test_lost_submission_response_prevents_online_retry(tmp_path):
    service = ComfyUIService({"base_url": "http://test.invalid", "workflows": [{"name": "t2i", "workflow": graph()}]}, tmp_path)
    service._request = AsyncMock(side_effect=OSError("lost response"))
    with pytest.raises(WorkflowError) as failure:
        await service.generate_image("t2i", {"prompt_text": "portrait"}, [])
    assert failure.value.submitted and failure.value.failure_stage == "submission"


def test_default_dimensions_do_not_override_reference_or_workflow_geometry():
    assert choose_dimensions({"aspect": "workflow", "square_size": "1024x1024"}, {}, "portrait") is None
    assert choose_dimensions({}, {"has_reference": True}, "portrait") is None
    assert choose_dimensions({"aspect": "workflow"}, {"image_size": "768x1024"}, "") == (768, 1024)
    assert choose_dimensions({}, {"request_text": "portrait 768x1024", "has_reference": True}, "") == (768, 1024)


def test_config_update_preserves_secret_and_future_settings():
    current = {"api_key": "secret", "future_option": 42}
    config = normalize_config(current, {"aspect": "workflow"})
    assert config["api_key"] == "secret" and config["future_option"] == 42
    assert public_config(config)["has_api_key"] and "api_key" not in public_config(config)
    assert normalize_config(config, {"api_key": ""})["api_key"] == ""
    with pytest.raises(WorkflowError):
        normalize_config(current, {"timeout_seconds": -1})


@pytest.mark.asyncio
async def test_failed_config_persistence_restores_previous_values(tmp_path):
    current = {"aspect": "workflow", "api_key": "secret"}
    plugin = SimpleNamespace(config={"comfyui": copy.deepcopy(current)}, data_dir=tmp_path, _save_config_if_possible=AsyncMock(return_value=False))
    api = ImageCompanionExtensionAPI(plugin)
    with pytest.raises(WorkflowError, match="保存失败"):
        await api.update_comfyui_config({"aspect": "portrait"})
    assert plugin.config["comfyui"] == current


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,order", [("auto", ["comfyui", "external", "sdgen"]), ("external_first", ["external", "comfyui", "sdgen"]), ("external", ["external"])])
@pytest.mark.parametrize("success", ["external", "comfyui", "sdgen", None])
async def test_backend_chain_order_and_single_backend_isolation(tmp_path, mode, order, success):
    runtime = _PhotoReliabilityHarness(tmp_path)
    runtime.photo_generation_backend = mode
    runtime.comfyui_text2img_workflow_name = "t2i"
    runtime._native_comfyui_service = lambda: None
    runtime._comfyui_photo_available = lambda: True
    runtime._sdgen_photo_available = lambda: True
    runtime._local_photo_generation_busy_state = lambda **kwargs: {}
    calls = []

    def backend(name):
        async def generate(*args, **kwargs):
            calls.append(name)
            return (str(runtime.generated_path), "ok") if name == success else ("", "preflight failure")
        return generate

    runtime._run_external_photo_generation = backend("external")
    runtime._run_comfyui_photo_workflow = backend("comfyui")
    runtime._run_sdgen_photo_generation = backend("sdgen")
    await runtime._generate_photo_image_legacy(workflow_kind="text2img", prompt_text="casual outfit portrait", session_key="routing-test")
    expected = order[:order.index(success) + 1] if success in order else order
    assert calls == expected


@pytest.mark.asyncio
async def test_online_first_stops_after_submitted_comfyui_failure(tmp_path):
    runtime = _PhotoReliabilityHarness(tmp_path)
    runtime.photo_generation_backend = "external_first"
    runtime.comfyui_text2img_workflow_name = "t2i"
    runtime._comfyui_photo_available = lambda: True
    runtime._sdgen_photo_available = lambda: True
    runtime._local_photo_generation_busy_state = lambda **kwargs: {}
    runtime._run_external_photo_generation = AsyncMock(return_value=("", "failed"))
    runtime._run_sdgen_photo_generation = AsyncMock()

    async def local(*args, **kwargs):
        runtime._native_comfyui_last_result = {"submitted": True, "task_id": "job", "failure_stage": "result"}
        return "", "timeout"

    runtime._run_comfyui_photo_workflow = local
    result = await runtime._generate_photo_image_legacy(workflow_kind="text2img", prompt_text="portrait", session_key="routing-test")
    assert result[0] == "ComfyUI"
    runtime._run_sdgen_photo_generation.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_skip_cannot_reuse_previous_submission_metadata(tmp_path):
    runtime = _PhotoReliabilityHarness(tmp_path)
    runtime.photo_generation_backend = "auto"
    runtime.comfyui_text2img_workflow_name = "t2i"
    runtime._comfyui_photo_available = lambda: True
    runtime._local_photo_generation_busy_state = lambda **kwargs: {"reason": "busy"}
    runtime._native_comfyui_last_result = {"submitted": True, "task_id": "previous-job"}
    result = await runtime._generate_photo_image_legacy(workflow_kind="text2img", prompt_text="portrait", session_key="routing-test")
    assert result[0] == "在线图片 API" and len(runtime.external_calls) == 1


@pytest.mark.parametrize("mode,external,comfyui,expected", [("external_first", False, True, True), ("auto", False, True, True), ("external", False, True, False), ("external_first", False, False, False)])
def test_chain_capability_respects_single_backend_mode(tmp_path, mode, external, comfyui, expected):
    runtime = _PhotoReliabilityHarness(tmp_path)
    runtime.photo_generation_backend = mode
    runtime._external_photo_available = lambda: external
    runtime._comfyui_photo_available = lambda: comfyui
    runtime._sdgen_photo_available = lambda: False
    runtime._custom_tool_photo_available = lambda: False
    runtime._backup_external_photo_available = lambda: False
    runtime._backup_external_unavailable_note = lambda: "disabled"
    assert ImageGenerationRuntime.capability_status(runtime)["available"] is expected
