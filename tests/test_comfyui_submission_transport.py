from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from astrbot_plugin_image_companion.comfyui_service import ComfyUIService
from astrbot_plugin_image_companion.comfyui_workflows import WorkflowError
from test_native_comfyui import graph
from test_migrated_photo_prompt_reliability import _PhotoReliabilityHarness


def service_at(tmp_path, base_url="http://test.invalid"):
    return ComfyUIService({
        "base_url": base_url, "rewrite_enabled": False, "aspect": "workflow",
        "workflows": [{"name": "t2i", "workflow": graph()}],
    }, tmp_path)


async def submit(service, entrypoint):
    if entrypoint == "generate_image":
        return await service.generate_image("t2i", {"prompt_text": "portrait"}, [])
    return await service.submit_generation("t2i", {"positive_prompt": "portrait"})


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["generate_image", "submit_generation"])
@pytest.mark.parametrize("error", [
    aiohttp.ClientConnectorError(
        SimpleNamespace(host="test.invalid", port=8188, ssl=False),
        ConnectionRefusedError("connection refused"),
    ),
    aiohttp.ConnectionTimeoutError("connect timed out"),
])
async def test_connection_failure_is_confirmed_not_submitted(tmp_path, entrypoint, error):
    service = service_at(tmp_path)
    service._request = AsyncMock(side_effect=error)
    with pytest.raises(WorkflowError, match="任务未提交") as failure:
        await submit(service, entrypoint)
    assert not failure.value.submitted
    assert failure.value.failure_stage == "connection"
    assert failure.value.__cause__ is error
    assert not service._tasks
    service._request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["generate_image", "submit_generation"])
@pytest.mark.parametrize("error", [
    aiohttp.ServerDisconnectedError("response lost"),
    aiohttp.SocketTimeoutError("read timed out"),
    aiohttp.ClientPayloadError("truncated response"),
    asyncio.TimeoutError("total deadline"),
    ConnectionResetError("connection reset"),
])
async def test_possible_submission_still_blocks_fallback(tmp_path, entrypoint, error):
    service = service_at(tmp_path)
    service._request = AsyncMock(side_effect=error)
    with pytest.raises(WorkflowError, match="提交结果不明") as failure:
        await submit(service, entrypoint)
    assert failure.value.submitted
    assert failure.value.failure_stage == "submission"
    assert failure.value.__cause__ is error
    service._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_deadline_precedes_total_deadline(tmp_path):
    service = service_at(tmp_path)
    try:
        session = await service._http()
        assert 0 < session.timeout.connect < session.timeout.total
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_offline_comfyui_falls_back_to_online_once(tmp_path):
    # Reserve a local port without listening: no remote server or paid API.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        service = service_at(tmp_path, f"http://127.0.0.1:{reserved.getsockname()[1]}")
        runtime = _PhotoReliabilityHarness(tmp_path)
        runtime.photo_generation_backend = "auto"
        runtime.comfyui_text2img_workflow_name = "t2i"
        runtime._native_comfyui_service = lambda: service
        runtime._comfyui_photo_available = lambda: True
        runtime._local_photo_generation_busy_state = lambda **kwargs: {}
        try:
            result = await runtime._generate_photo_image_legacy(
                workflow_kind="text2img", prompt_text="portrait", session_key="transport-test",
            )
            assert result[0] == "在线图片 API"
            assert len(runtime.external_calls) == 1
            assert runtime._native_comfyui_last_result["submitted"] is False
            assert runtime._native_comfyui_last_result["failure_stage"] == "connection"
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_accepted_request_with_lost_response_does_not_fall_back(tmp_path):
    received = []

    async def accept_then_disconnect(request):
        received.append(await request.json())
        request.transport.close()
        return web.json_response({"prompt_id": "job"})

    app = web.Application()
    app.router.add_post("/prompt", accept_then_disconnect)
    runner = web.AppRunner(app)
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    service = service_at(tmp_path, f"http://127.0.0.1:{listener.getsockname()[1]}")
    try:
        await web.SockSite(runner, listener).start()
        runtime = _PhotoReliabilityHarness(tmp_path)
        runtime.photo_generation_backend = "auto"
        runtime.comfyui_text2img_workflow_name = "t2i"
        runtime._native_comfyui_service = lambda: service
        runtime._comfyui_photo_available = lambda: True
        runtime._local_photo_generation_busy_state = lambda **kwargs: {}
        result = await runtime._generate_photo_image_legacy(
            workflow_kind="text2img", prompt_text="portrait", session_key="transport-test",
        )
        assert len(received) == 1
        assert result[0] == "ComfyUI"
        assert not runtime.external_calls
        assert runtime._native_comfyui_last_result["submitted"] is True
    finally:
        await service.close()
        await runner.cleanup()
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["generate_image", "submit_generation"])
async def test_missing_task_id_is_not_a_safe_retry(tmp_path, entrypoint):
    service = service_at(tmp_path)
    service._request = AsyncMock(return_value={})
    with pytest.raises(WorkflowError) as failure:
        await submit(service, entrypoint)
    assert failure.value.submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["generate_image", "submit_generation"])
async def test_oversized_submission_response_is_not_a_safe_retry(tmp_path, entrypoint):
    service = service_at(tmp_path)

    async def chunks():
        yield b"x" * (8 * 1024 * 1024 + 1)

    @asynccontextmanager
    async def response(*args, **kwargs):
        yield SimpleNamespace(content=SimpleNamespace(iter_chunked=lambda size: chunks()), status=200)

    service._http = AsyncMock(return_value=SimpleNamespace(request=response))
    with pytest.raises(WorkflowError, match="返回内容过大") as failure:
        await submit(service, entrypoint)
    assert failure.value.submitted
    assert failure.value.failure_stage == "submission"
    assert not service._tasks


@pytest.mark.asyncio
async def test_submission_cancellation_propagates(tmp_path):
    service = service_at(tmp_path)
    service._request = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await submit(service, "generate_image")
