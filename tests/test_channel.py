"""Real HTTP and native channel execution with a deterministic Agent stub."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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


def make_channel(port, workspace_dir=None):
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
            from qwenpaw.app.approvals import (
                ApprovalRequestSummary,
                get_approval_service,
            )

            approvals = get_approval_service()
            pending = await approvals.create_pending_summary(
                session_id=request.session_id,
                root_session_id=request.session_id,
                owner_agent_id="test-agent",
                user_id=request.user_id,
                channel=channel.channel,
                agent_id="test-agent",
                summary=ApprovalRequestSummary(
                    source_type="tool_guard",
                    name="shell",
                    severity="high",
                    result_summary="Writes outside the workspace",
                    payload={
                        "display": {
                            "tool_name": "shell",
                            "exact_target": "rm -rf build",
                            "similar_target": "rm -rf *",
                            "is_generalized": True,
                        }
                    },
                ),
                extra={"_channel_instance": channel},
                timeout_seconds=30,
            )
            decision = await approvals.wait_for_approval(
                pending,
                timeout_seconds=10,
            )
            yield Event(
                object="message",
                type=MessageType.MESSAGE,
                status=RunStatus.Completed,
                content=[
                    TextContent(text=f"Approval outcome: {decision.value}")
                ],
            )
            yield Event(object="response", status=RunStatus.Completed)
            return
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
            workspace_dir=workspace_dir,
        ),
        SimpleNamespace(is_control_command=lambda text: False),
    )
    return channel, calls, gates


@pytest_asyncio.fixture
async def service(unused_tcp_port, monkeypatch, tmp_path):
    # Isolate config before the first native import.
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(tmp_path))
    channel, calls, gates = make_channel(
        unused_tcp_port, tmp_path / "workspace"
    )
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


async def wait_state(service, request_id, expected):
    async with asyncio.timeout(5):
        while service.channel._requests[request_id].state != expected:
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
    assert caps["recovery"] is False
    assert caps["approvals"] is True and caps["approvalEvents"] is False
    assert caps["attachments"] is True and caps["maxAttachments"] == 4
    assert caps["maxContextChars"] == 30000
    with patch(
        "_page_assist_test_plugin.channel.load_agent_config_async",
        new=AsyncMock(return_value=SimpleNamespace(name="Research Agent")),
    ):
        response = await service.client.get(service.prefix + "/agents")
    assert (await response.json())["agents"] == [
        {"id": "test-agent", "name": "Research Agent"},
    ]
    with patch(
        "_page_assist_test_plugin.channel.load_agent_config_async",
        new=AsyncMock(side_effect=RuntimeError("config unavailable")),
    ):
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
    await wait_state(service, payload["requestId"], "waiting_approval")
    assert await service.channel._workspace.task_tracker.has_active_tasks()
    response = await service.client.post(
        f"{service.prefix}/approvals/{uuid4()}/decision",
        json={"decisionId": str(uuid4()), "approved": True, "scope": "exact"},
    )
    assert response.status == 404
    await service.channel.stop()
    assert not await service.channel._workspace.task_tracker.has_active_tasks()
    assert service.channel._requests[payload["requestId"]].state == "cancelled"
    # Leave no unresolved native approval behind for later tests.
    from qwenpaw.app.approvals import get_approval_service
    from qwenpaw.security.tool_guard.approval import ApprovalDecision

    for pending in await get_approval_service().get_all_pending_by_session(
        service.calls[0].session_id
    ):
        await get_approval_service().resolve_request(
            pending.request_id, ApprovalDecision.TIMEOUT
        )


@pytest.mark.asyncio
async def test_browser_approval_list_and_decision(service):
    conversation = await create(service)
    payload = await submit(service, conversation, "approval")
    await wait_state(service, payload["requestId"], "waiting_approval")

    response = await service.client.get(f"{service.prefix}/approvals")
    rows = (await response.json())["approvals"]
    assert len(rows) == 1
    row = rows[0]
    assert row["conversationId"] == conversation
    assert row["agentId"] == "test-agent"
    assert row["requestId"] == payload["requestId"]
    assert row["tool"] == "shell"
    assert row["target"] == "rm -rf build"
    assert row["risk"] == "Writes outside the workspace"
    assert row["actionable"] is True
    assert [scope["id"] for scope in row["scopes"]] == ["exact", "similar"]
    # The panel renders "Approve — {label}", so labels only name the scope.
    assert [scope["label"] for scope in row["scopes"]] == [
        "Exact target",
        "Similar pattern",
    ]
    assert all(scope["description"] for scope in row["scopes"])
    expires_at = datetime.fromisoformat(row["expiresAt"])
    assert expires_at > datetime.now(timezone.utc)

    path = f"{service.prefix}/approvals/{row['id']}/decision"
    decision = {"decisionId": str(uuid4()), "approved": True, "scope": "exact"}
    response = await service.client.post(path, json=decision)
    assert response.status == 200
    assert await response.json() == {
        "approvalId": row["id"],
        "decisionId": decision["decisionId"],
        "approved": True,
        "scope": "exact",
        "status": "confirmed",
    }
    response = await service.client.post(path, json=decision)
    assert response.status == 200
    response = await service.client.post(
        path, json={**decision, "approved": False}
    )
    assert response.status == 409

    assert await wait_done(service, payload["requestId"]) == "completed"
    response = await service.client.get(f"{service.prefix}/approvals")
    assert (await response.json())["approvals"] == []
    response = await service.client.get(
        f"{service.prefix}/conversations/{conversation}/messages"
    )
    contents = [m["content"] for m in (await response.json())["messages"]]
    # Approvals surface through the composer button, never as chat messages.
    assert not any("Native tool approval is required" in c for c in contents)
    assert "Approval outcome: approved" in contents


@pytest.mark.asyncio
async def test_approval_ownership_deny_and_scope_guards(service):
    from qwenpaw.app.approvals import (
        ApprovalRequestSummary,
        get_approval_service,
    )
    from qwenpaw.security.tool_guard.approval import ApprovalDecision

    conversation = await create(service)
    payload = await submit(service, conversation, "wait")
    async with asyncio.timeout(5):
        while not service.calls:
            await asyncio.sleep(0.01)
    session_id = service.calls[0].session_id
    approvals = get_approval_service()

    async def raise_pending(channel, session, summary):
        return await approvals.create_pending_summary(
            session_id=session,
            root_session_id=session,
            owner_agent_id="test-agent",
            user_id=service.channel._user,
            channel=channel,
            agent_id="test-agent",
            summary=ApprovalRequestSummary(
                source_type="tool_guard",
                name="shell",
                severity="medium",
                result_summary=summary,
            ),
            timeout_seconds=30,
        )

    own = await raise_pending("page_assist", session_id, "Reads a secret")
    elsewhere = await raise_pending("telegram", session_id, "Other channel")
    unknown = await raise_pending("page_assist", "page_assist:missing", "Gone")

    response = await service.client.get(f"{service.prefix}/approvals")
    rows = (await response.json())["approvals"]
    assert [row["id"] for row in rows] == [own.request_id]
    assert rows[0]["requestId"] == payload["requestId"]
    assert rows[0]["target"] == "shell"
    assert rows[0]["risk"] == "Reads a secret"
    assert [scope["id"] for scope in rows[0]["scopes"]] == ["exact"]

    path = f"{service.prefix}/approvals/{own.request_id}/decision"
    similar = {
        "decisionId": str(uuid4()),
        "approved": True,
        "scope": "similar",
    }
    response = await service.client.post(path, json=similar)
    assert response.status == 400
    for foreign in (elsewhere, unknown):
        response = await service.client.post(
            f"{service.prefix}/approvals/{foreign.request_id}/decision",
            json={"decisionId": str(uuid4()), "approved": False},
        )
        assert response.status == 404
    response = await service.client.post(
        path, json={"decisionId": str(uuid4()), "approved": False}
    )
    assert response.status == 200
    assert await approvals.get_request(own.request_id) is None

    for foreign in (elsewhere, unknown):
        await approvals.resolve_request(
            foreign.request_id, ApprovalDecision.DENIED
        )
    service.gates[session_id].set()
    assert await wait_done(service, payload["requestId"]) == "completed"


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


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pixel" * 8
PDF_BYTES = b"%PDF-1.4 deterministic fake body"


async def upload(service, name, data, media_type=""):
    response = await service.client.post(
        service.prefix + "/attachments",
        json={
            "name": name,
            "mediaType": media_type,
            "data": base64.b64encode(data).decode(),
        },
    )
    return response


@pytest.mark.asyncio
async def test_attachments_reach_the_agent_as_file_contents(service, tmp_path):
    media = tmp_path / "workspace" / "media" / "page_assist"
    image = await upload(service, "../../evil:pic.png", PNG_BYTES, "image/png")
    assert image.status == 201
    image_body = await image.json()
    assert image_body["name"] == "evil_pic.png"
    assert image_body["kind"] == "image"
    assert image_body["mediaType"] == "image/png"
    document = await upload(
        service, "report.pdf", PDF_BYTES, "application/pdf"
    )
    assert document.status == 201
    document_body = await document.json()
    assert document_body["kind"] == "file"
    assert (media / f"{image_body['attachmentId']}.png").read_bytes() == (
        PNG_BYTES
    )
    conversation = await create(service)
    payload = {
        "requestId": str(uuid4()),
        "text": "look at these",
        "attachments": [
            {"attachmentId": image_body["attachmentId"]},
            {"attachmentId": document_body["attachmentId"]},
        ],
    }
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json=payload,
    )
    assert response.status == 202, await response.text()
    assert await wait_done(service, payload["requestId"]) == "completed"
    contents = service.calls[-1].input[0].content
    assert contents[0].text == "look at these"
    assert (
        contents[1].image_url
        == (media / f"{image_body['attachmentId']}.png").resolve().as_uri()
    )
    assert (
        contents[2].file_url
        == (media / f"{document_body['attachmentId']}.pdf").resolve().as_uri()
    )
    assert contents[2].filename == "report.pdf"
    response = await service.client.get(
        f"{service.prefix}/conversations/{conversation}/messages",
    )
    history = await response.json()
    user_text = history["messages"][0]["content"]
    assert "[attachment image: evil_pic.png]" in user_text
    assert "[attachment file: report.pdf]" in user_text
    # A consumed attachment can never ride a second request.
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json={
            "requestId": str(uuid4()),
            "text": "reuse",
            "attachments": [
                {"attachmentId": image_body["attachmentId"]}
            ],
        },
    )
    assert response.status == 400
    # An unknown id is rejected the same way.
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json={
            "requestId": str(uuid4()),
            "text": "ghost",
            "attachments": [{"attachmentId": str(uuid4())}],
        },
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_attachment_upload_validation(service):
    response = await upload(service, "empty.png", b"")
    assert response.status == 400
    response = await service.client.post(
        service.prefix + "/attachments",
        json={"name": "x.png", "mediaType": "", "data": "not!!base64"},
    )
    assert response.status == 400
    oversized = base64.b64encode(
        b"a" * (5 * 1024 * 1024 + 1)
    ).decode()
    response = await service.client.post(
        service.prefix + "/attachments",
        json={"name": "big.bin", "mediaType": "", "data": oversized},
    )
    assert response.status == 400
    response = await upload(service, " ", PNG_BYTES)
    assert response.status == 400
    # More than four references fail validation before anything claims them.
    conversation = await create(service)
    uploaded = await upload(service, "one.png", PNG_BYTES)
    uploaded_id = (await uploaded.json())["attachmentId"]
    response = await service.client.post(
        f"{service.prefix}/conversations/{conversation}/messages",
        json={
            "requestId": str(uuid4()),
            "text": "too many",
            "attachments": [{"attachmentId": uploaded_id}]
            + [{"attachmentId": str(uuid4())} for _ in range(4)],
        },
    )
    assert response.status == 400
    assert uploaded_id not in {
        a.id for a in service.channel._attachments.values() if a.used_by
    }


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
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            # Windows event loops reject add_signal_handler; fall back to
            # signal.signal so the harness can stop the channel there too.
            signal.signal(
                sig, lambda *_: loop.call_soon_threadsafe(stopped.set)
            )
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
