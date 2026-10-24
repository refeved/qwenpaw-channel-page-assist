"""Real HTTP and native channel execution with a deterministic Agent stub."""

import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio

PLUGIN = Path(__file__).resolve().parents[1]
TOKEN = "page-assist-test-token-not-for-production"


def load_plugin():
    name = "_page_assist_test_plugin"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name,
            PLUGIN / "plugin.py",
            submodule_search_locations=[str(PLUGIN)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    api = SimpleNamespace(
        register_channel=lambda **kw: setattr(api, "registration", kw)
    )
    sys.modules[name].plugin.register(api)
    return api.registration["channel_class"]


class MemoryChats:
    def __init__(self):
        self.chats = {}

    async def get_or_create_chat(self, session_id, user_id, channel, **kwargs):
        key = (session_id, user_id, channel)
        return self.chats.setdefault(key, SimpleNamespace(id=str(uuid4())))

    async def touch_chat(self, chat_id):
        pass

    async def mark_chat_finished(self, chat_id, finished_at):
        pass


def make_channel(port):
    from qwenpaw.app.task_tracker import TaskTracker
    from qwenpaw.schemas import Event, MessageType, RunStatus, TextContent

    channel_class = load_plugin()
    calls = []
    gates = {}
    remembered = {}

    async def process(request):
        text = request.input[0].content[0].text
        calls.append(request)
        if text == "wait":
            await gates.setdefault(request.session_id, asyncio.Event()).wait()
        if text == "error":
            raise RuntimeError("deterministic provider failure")
        if text == "approval":
            await channel.send_approval_notification(
                session_id=request.session_id,
                user_id=request.user_id,
                request_id="native-approval-test",
                tool_name="test_tool",
                severity="high",
                result_summary="not browser-approved",
            )
            await gates.setdefault(request.session_id, asyncio.Event()).wait()
        count = remembered.get(request.session_id, 0) + 1
        remembered[request.session_id] = count
        yield Event(
            object="message",
            type=MessageType.REASONING,
            status=RunStatus.Completed,
            content=[TextContent(text="HIDDEN_REASONING")],
        )
        yield Event(
            object="message",
            type=MessageType.MESSAGE,
            status=RunStatus.Completed,
            content=[TextContent(text=f"Native reply {count}: {text}")],
        )
        yield Event(object="response", status=RunStatus.Completed)

    # Tool discovery belongs to the full runtime, not this transport fixture.
    render_config = SimpleNamespace(tools=SimpleNamespace(builtin_tools={}))
    with patch(
        "qwenpaw.app.channels.base.load_config", return_value=render_config
    ):
        channel = channel_class(
            process,
            enabled=True,
            browser_token=TOKEN,
            http_port=port,
        )
    channel.set_workspace(
        SimpleNamespace(
            agent_id="test-agent",
            chat_manager=MemoryChats(),
            task_tracker=TaskTracker(),
        ),
        SimpleNamespace(is_control_command=lambda text: False),
    )
    return channel, calls, gates


@pytest_asyncio.fixture
async def service(unused_tcp_port, monkeypatch, tmp_path):
    # Isolate config before the first native import.
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path))
    channel, calls, gates = make_channel(unused_tcp_port)
    await channel.start()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(
        base_url=f"http://127.0.0.1:{unused_tcp_port}",
        headers=headers,
    ) as client:
        try:
            yield SimpleNamespace(
                client=client,
                channel=channel,
                calls=calls,
                gates=gates,
                prefix="/api/page-assist",
            )
        finally:
            await channel.stop()


async def create(service):
    response = await service.client.post(
        f"{service.prefix}/conversations",
        json={"agentId": "test-agent"},
    )
    assert response.status == 200
    return (await response.json())["conversationId"]


async def submit(service, conversation, text, **extra):
    payload = {"requestId": str(uuid4()), "text": text, **extra}
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json=payload,
    )
    assert response.status == 202, await response.text()
    return payload


async def wait_done(service, request_id):
    async with asyncio.timeout(10):
        while True:
            response = await service.client.get(
                f"{service.prefix}/requests/{request_id}"
            )
            state = (await response.json())["state"]
            if state in {"completed", "failed", "cancelled", "unknown"}:
                return state
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_native_chat_and_follow_up(service):
    dispatches = []

    async def dispatched(*args):
        dispatches.append(args)

    service.channel._on_reply_sent = dispatched
    conversation = await create(service)
    first = await submit(service, conversation, "hello")
    assert await wait_done(service, first["requestId"]) == "completed"
    second = await submit(service, conversation, "follow up")
    assert await wait_done(service, second["requestId"]) == "completed"
    assert service.calls[0].session_id == service.calls[1].session_id
    assert service.calls[0].channel == "page_assist"
    assert service.calls[0].channel_instance is service.channel
    assert dispatches[0] == (
        "page_assist",
        service.calls[0].user_id,
        service.calls[0].session_id,
    )
    other = await create(service)
    third = await submit(service, other, "separate chat")
    assert await wait_done(service, third["requestId"]) == "completed"
    assert service.calls[2].session_id != service.calls[0].session_id
    response = await service.client.get(
        f"{service.prefix}/conversations/{conversation}/messages",
    )
    history = await response.json()
    assert history["complete"] is True
    assert len(history["messages"]) == 4
    assert history["messages"][-1]["content"] == "Native reply 2: follow up"
    assert "HIDDEN_REASONING" not in json.dumps(history)
    response = await service.client.get(
        f"{service.prefix}/requests/{second['requestId']}/events",
    )
    events = [
        json.loads(line[6:])
        for line in (await response.text()).splitlines()
        if line.startswith("data: ")
    ]
    assert [e["type"] for e in events] == [
        "request.state",
        "message.snapshot",
        "request.terminal",
    ]
    assert events[-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_auth_scope_and_capabilities(service):
    for endpoint in ["/capabilities", "/agents", "/requests/anything"]:
        response = await service.client.get(
            service.prefix + endpoint,
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status == 401
    response = await service.client.get(service.prefix + "/capabilities")
    caps = await response.json()
    assert caps["recovery"] is False and caps["approvals"] is False
    assert caps["maxContextChars"] == 30000
    response = await service.client.get(service.prefix + "/agents")
    assert (await response.json())["agents"] == [
        {"id": "test-agent", "name": "test-agent"},
    ]
    response = await service.client.post(
        service.prefix + "/conversations",
        json={"agentId": "another-agent"},
    )
    assert response.status == 403
    response = await service.client.get(
        service.prefix + "/conversations/missing/messages"
    )
    assert response.status == 404
    replacement, _, _ = make_channel(8766)
    assert replacement._instance != caps["instanceId"]


@pytest.mark.asyncio
async def test_busy_dedup_and_disconnect(service):
    conversation = await create(service)
    payload = await submit(service, conversation, "wait")
    path = f"{service.prefix}/conversations/{conversation}/messages"
    response = await service.client.post(path, json=payload)
    assert response.status == 200
    response = await service.client.post(
        path, json={**payload, "text": "different"}
    )
    assert response.status == 409
    response = await service.client.post(
        path, json={"requestId": str(uuid4()), "text": "queued?"}
    )
    assert response.status == 409
    async with asyncio.timeout(5):
        while not service.calls:
            await asyncio.sleep(0.01)
    response = await service.client.get(
        f"{service.prefix}/requests/{payload['requestId']}/events",
    )
    await response.content.readline()
    response.close()
    assert await service.channel._workspace.task_tracker.has_active_tasks()
    service.gates[service.calls[0].session_id].set()
    assert await wait_done(service, payload["requestId"]) == "completed"
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_webpage_validation_and_native_delivery(service):
    conversation = await create(service)
    url, mode, content = (
        "https://example.com/article",
        "selection",
        "Shared 中文 😀",
    )
    digest = hashlib.sha256(
        json.dumps(
            [url, mode, content],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    context = {
        "type": "webpage",
        "mode": mode,
        "title": "Example",
        "url": url,
        "capturedAt": "2026-10-24T00:00:00Z",
        "content": content,
        "contentHash": f"sha256:{digest}",
        "truncated": False,
    }
    payload = await submit(
        service, conversation, "summarize", pageContext=context
    )
    assert await wait_done(service, payload["requestId"]) == "completed"
    native_text = service.calls[0].input[0].content[0].text
    assert "summarize" in native_text and content in native_text
    assert "reference data" in native_text
    path = f"{service.prefix}/conversations/{conversation}/messages"
    for changes in [
        {"contentHash": "sha256:wrong"},
        {"url": url + "?secret=x"},
        {"content": "a" * 30001},
        {"content": "😀" * 15001},
    ]:
        response = await service.client.post(
            path,
            json={
                "requestId": str(uuid4()),
                "text": "bad",
                "pageContext": {**context, **changes},
            },
        )
        assert response.status == 400
    for text in ["", "a" * 8001, "😀" * 4001]:
        response = await service.client.post(
            path, json={"requestId": str(uuid4()), "text": text}
        )
        assert response.status == 400
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_errors_are_not_success(service):
    conversation = await create(service)
    payload = await submit(service, conversation, "error")
    assert await wait_done(service, payload["requestId"]) == "failed"
    response = await service.client.get(
        f"{service.prefix}/conversations/{conversation}/messages",
    )
    assert any(
        "error" in m["content"].lower()
        for m in (await response.json())["messages"]
    )


@pytest.mark.asyncio
async def test_approval_waits_for_native_action_and_stop_cleans_tasks(service):
    conversation = await create(service)
    payload = await submit(service, conversation, "approval")
    async with asyncio.timeout(5):
        while (
            service.channel._requests[payload["requestId"]].state
            != "waiting_approval"
        ):
            await asyncio.sleep(0.01)
    assert await service.channel._workspace.task_tracker.has_active_tasks()
    response = await service.client.post(
        f"{service.prefix}/approvals/native-approval-test/decision",
        json={"approved": True},
    )
    assert response.status == 404
    await service.channel.stop()
    assert not await service.channel._workspace.task_tracker.has_active_tasks()
    assert service.channel._requests[payload["requestId"]].state == "cancelled"


@pytest.mark.asyncio
async def test_invalid_configuration_and_port_collision(service):
    channel_class = type(service.channel)
    render_config = SimpleNamespace(tools=SimpleNamespace(builtin_tools={}))
    with patch(
        "qwenpaw.app.channels.base.load_config", return_value=render_config
    ):
        invalid = channel_class(
            service.channel._process, enabled=True, browser_token="short"
        )
    invalid.set_workspace(
        service.channel._workspace, service.channel._command_registry
    )
    with pytest.raises(ValueError, match="token"):
        await invalid.start()
    collision, _, _ = make_channel(service.channel._port)
    with pytest.raises(OSError):
        await collision.start()
    assert collision._runner is None


@pytest.mark.asyncio
async def test_exact_limits_and_capacity(service):
    conversation = await create(service)
    text = "😀" * 4000
    url, mode, content = "https://example.com", "article", "😀" * 15000
    digest = hashlib.sha256(
        json.dumps(
            [url, mode, content],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    payload = await submit(
        service,
        conversation,
        text,
        pageContext={
            "type": "webpage",
            "mode": mode,
            "title": "Limit",
            "url": url,
            "capturedAt": "2026-10-24T00:00:00Z",
            "content": content,
            "contentHash": f"sha256:{digest}",
            "truncated": True,
        },
    )
    assert await wait_done(service, payload["requestId"]) == "completed"
    for _ in range(99):
        await create(service)
    response = await service.client.post(
        service.prefix + "/conversations",
        json={"agentId": "test-agent"},
    )
    assert response.status == 429
    record = service.channel._requests[payload["requestId"]]
    for index in range(499):
        service.channel._requests[f"capacity-{index}"] = record
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json={"requestId": str(uuid4()), "text": "over capacity"},
    )
    assert response.status == 429
    assert len(service.calls) == 1


def test_manifest_and_offline_install_validation():
    from qwenpaw.plugins.architecture import PluginManifest, PluginType
    from qwenpaw.plugins.validation import validate_plugin_module

    manifest = PluginManifest.model_validate_json(
        (PLUGIN / "plugin.json").read_text()
    )
    assert manifest.plugin_type == PluginType.CHANNEL
    assert manifest.entry.backend == "plugin.py"
    validate_plugin_module(manifest.id, PLUGIN, manifest.entry.backend)
    assert load_plugin().uses_manager_queue is False


async def serve(port):
    channel, _, _ = make_channel(port)
    await channel.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    print("CHANNEL_TEST_READY", flush=True)
    try:
        await stopped.wait()
    finally:
        await channel.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="page-assist-channel-"
    ) as working_dir:
        os.environ["QWENPAW_WORKING_DIR"] = working_dir
        asyncio.run(serve(int(sys.argv[1])))
