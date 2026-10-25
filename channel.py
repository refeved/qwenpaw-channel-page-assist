"""Single-Agent HTTP transport over QwenPaw's native channel pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aiohttp import web
from qwenpaw.app.channels.base import BaseChannel
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.config.config import load_agent_config_async
from qwenpaw.schemas import MessageType, TextContent

from .protocol import MAX_CONTEXT, MAX_TEXT, MessagePayload, NewConversation

logger = logging.getLogger(__name__)
PREFIX = "/api/page-assist"
TERMINAL = {"completed", "failed", "cancelled"}


@dataclass
class Conversation:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    active: str | None = None


@dataclass
class Request:
    payload: MessagePayload
    conversation: Conversation
    state: str = "accepted"
    completed: bool = False
    error: str | None = None
    task: asyncio.Task | None = None
    chat_id: str | None = None

    def dto(self):
        return {
            "requestId": self.payload.requestId,
            "conversationId": self.conversation.id,
            "state": self.state,
        }


class PageAssistChannel(BaseChannel):
    channel = "page_assist"
    # Reject busy conversations rather than merging queued turns.
    uses_manager_queue = False

    def __init__(
        self,
        process,
        *,
        enabled=False,
        browser_token="",
        http_host="127.0.0.1",
        http_port=8766,
        on_reply_sent=None,
    ):
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            display_config=ChannelDisplayConfig(
                show_thinking=False,
                show_tool_details=False,
                show_tool_calls=False,
                show_tool_results=False,
            ),
        )
        self.enabled = enabled
        self._token = browser_token
        self._host = http_host
        self._port = int(http_port)
        self._instance = str(uuid4())
        self._user = hashlib.sha256(browser_token.encode()).hexdigest()
        self._conversations: dict[str, Conversation] = {}
        self._requests: dict[str, Request] = {}
        self._runner: web.AppRunner | None = None
        self._stopping = False

    @classmethod
    def from_config(
        cls,
        process,
        config,
        on_reply_sent=None,
        **kwargs,
    ):
        def read(key, default):
            if isinstance(config, dict):
                return config.get(key, default)
            return getattr(config, key, default)

        return cls(
            process,
            enabled=read("enabled", False),
            browser_token=read("browser_token", ""),
            http_host=read("http_host", "127.0.0.1"),
            http_port=read("http_port", 8766),
            on_reply_sent=on_reply_sent,
        )

    def clone(self, config):
        return self.from_config(self._process, config, self._on_reply_sent)

    @property
    def agent_id(self):
        if self._workspace is None:
            raise RuntimeError("Page Assist channel has no Agent workspace")
        return self._workspace.agent_id

    async def start(self):
        if not self.enabled or self._runner is not None:
            return
        if (
            not isinstance(self._token, str)
            or len(self._token) < 32
            or not self._token.isascii()
            or any(c.isspace() for c in self._token)
        ):
            raise ValueError(
                "Page Assist requires an ASCII token "
                "of at least 32 characters without whitespace"
            )
        if not 1 <= self._port <= 65535:
            raise ValueError("Page Assist port must be between 1 and 65535")
        if not self.agent_id or self._command_registry is None:
            raise RuntimeError(
                "Page Assist requires the native workspace "
                "and command registry"
            )
        self._stopping = False
        app = web.Application(
            middlewares=[self._authenticate],
            client_max_size=256 * 1024,
        )
        app.add_routes(
            [
                web.get(f"{PREFIX}/capabilities", self._capabilities),
                web.get(f"{PREFIX}/agents", self._agents),
                web.post(f"{PREFIX}/conversations", self._create_conversation),
                web.post(
                    f"{PREFIX}/conversations/{{id}}/messages", self._submit
                ),
                web.get(
                    f"{PREFIX}/conversations/{{id}}/messages", self._history
                ),
                web.get(f"{PREFIX}/requests/{{id}}", self._request),
                web.get(f"{PREFIX}/requests/{{id}}/events", self._events),
            ]
        )
        runner = web.AppRunner(app, shutdown_timeout=2)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self._port).start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        logger.info(
            "Page Assist channel listening on %s:%s for Agent %s",
            self._host,
            self._port,
            self.agent_id,
        )

    async def stop(self):
        self._stopping = True
        for record in self._requests.values():
            if record.chat_id and record.state not in TERMINAL:
                await self._workspace.task_tracker.request_stop(
                    record.chat_id,
                    reason="page_assist_channel_stop",
                )
        tasks = [
            r.task
            for r in self._requests.values()
            if r.task and not r.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _authenticate(self, request, handler):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(
            supplied.encode(), f"Bearer {self._token}".encode()
        ):
            raise web.HTTPUnauthorized(
                text="Invalid Page Assist browser token"
            )
        if self._stopping:
            raise web.HTTPServiceUnavailable(text="Channel is stopping")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _body(self, request, model):
        try:
            data = await request.json()
            return model.model_validate(data)
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid browser request payload"
            ) from None

    async def _capabilities(self, request):
        return web.json_response(
            {
                "protocolVersion": 1,
                "instanceId": self._instance,
                "principalScope": self._user,
                "maxContextChars": MAX_CONTEXT,
                "maxTextChars": MAX_TEXT,
                "recovery": False,
                "approvals": False,
                "approvalEvents": False,
            }
        )

    async def _agent_name(self):
        """Prefer the name configured in QwenPaw; fall back to the Agent id."""
        try:
            config = await load_agent_config_async(self.agent_id)
            return (config.name or "").strip() or self.agent_id
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Could not read the configured name for Agent %s",
                self.agent_id,
                exc_info=True,
            )
            return self.agent_id

    async def _agents(self, request):
        return web.json_response(
            {
                "agents": [{"id": self.agent_id, "name": await self._agent_name()}],
            }
        )

    async def _create_conversation(self, request):
        body = await self._body(request, NewConversation)
        if body.agentId != self.agent_id:
            raise web.HTTPForbidden(
                text="This channel is bound to another Agent"
            )
        if len(self._conversations) >= 100:
            raise web.HTTPTooManyRequests(
                text="Channel conversation limit reached; "
                "restart to clear browser state"
            )
        conversation = Conversation(str(uuid4()))
        self._conversations[conversation.id] = conversation
        return web.json_response(
            {
                "conversationId": conversation.id,
                "agentId": self.agent_id,
            }
        )

    def _conversation(self, request):
        conversation = self._conversations.get(request.match_info["id"])
        if conversation is None:
            raise web.HTTPNotFound(
                text="Conversation unavailable; "
                "create a new chat after channel restart"
            )
        return conversation

    def _find_request(self, request):
        record = self._requests.get(request.match_info["id"])
        if record is None:
            raise web.HTTPNotFound(
                text="Request unavailable in this channel instance"
            )
        return record

    async def _submit(self, request):
        conversation = self._conversation(request)
        payload = await self._body(request, MessagePayload)
        existing = self._requests.get(payload.requestId)
        if existing:
            if (
                existing.conversation is not conversation
                or existing.payload != payload
            ):
                raise web.HTTPConflict(
                    text="requestId already used with another payload"
                )
            return web.json_response(existing.dto())
        if conversation.active:
            raise web.HTTPConflict(text="Conversation is busy")
        if len(self._requests) >= 500:
            raise web.HTTPTooManyRequests(
                text="Channel request limit reached; "
                "restart to clear browser state"
            )
        record = Request(payload, conversation)
        self._requests[payload.requestId] = record
        conversation.active = payload.requestId
        conversation.messages.append(
            {
                "id": f"{payload.requestId}:user",
                "requestId": payload.requestId,
                "role": "user",
                "content": payload.text,
                "status": "complete",
            }
        )
        record.task = asyncio.create_task(self._execute(record))
        return web.json_response(record.dto(), status=202)

    async def _history(self, request):
        conversation = self._conversation(request)
        return web.json_response(
            {
                "conversationId": conversation.id,
                "complete": True,
                "messages": conversation.messages,
            }
        )

    async def _request(self, request):
        return web.json_response(self._find_request(request).dto())

    async def _events(self, request):
        record = self._find_request(request)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        async def emit(event):
            event["requestId"] = record.payload.requestId
            await response.write(f"data: {json.dumps(event)}\n\n".encode())

        seen = set()
        state = None
        try:
            while not self._stopping:
                if state != record.state:
                    state = record.state
                    await emit({"type": "request.state", "state": state})
                for message in record.conversation.messages:
                    if (
                        message["role"] != "assistant"
                        or message["requestId"] != record.payload.requestId
                        or message["id"] in seen
                    ):
                        continue
                    await emit(
                        {
                            "type": "message.snapshot",
                            "messageId": message["id"],
                            "content": message["content"],
                            "status": message["status"],
                        }
                    )
                    seen.add(message["id"])
                if record.state in TERMINAL or record.state == "unknown":
                    await emit(
                        {"type": "request.terminal", "state": record.state}
                    )
                    break
                await response.write(b": waiting\n\n")
                await asyncio.sleep(1)
        except (ConnectionResetError, BrokenPipeError):
            logger.debug(
                "Page Assist subscriber disconnected; native run continues"
            )
        return response

    def get_to_handle_from_request(self, request):
        return request.session_id.removeprefix(f"{self.channel}:")

    def get_on_reply_sent_args(self, request, to_handle):
        return request.user_id, request.session_id

    async def _execute(self, record):
        record.state = "running"
        try:
            request = self.build_agent_request_from_user_content(
                self.channel,
                self._user,
                f"{self.channel}:{record.conversation.id}",
                [TextContent(text=record.payload.agent_text())],
            )
            request.channel_meta = {"conversation_id": record.conversation.id}
            request.channel_instance = self
            chat = await self._workspace.chat_manager.get_or_create_chat(
                request.session_id,
                self._user,
                self.channel,
                name=record.payload.text,
            )
            record.chat_id = chat.id
            await self.consume_one(request)
            if self._stopping:
                record.state = "cancelled"
            elif record.error:
                record.state = "failed"
            elif record.completed:
                record.state = "completed"
            else:
                record.state = "unknown"
                await self.send(
                    record.conversation.id,
                    "Native channel did not confirm completion. "
                    "Do not resend; check QwenPaw Console.",
                )
        except asyncio.CancelledError:
            record.state = "cancelled"
            raise
        except Exception:
            logger.exception("Page Assist channel execution failed")
            record.state = "failed"
            await self.send(
                record.conversation.id,
                "Native channel execution failed; check QwenPaw logs.",
            )
        finally:
            if record.state != "unknown":
                record.conversation.active = None

    def _record_for_request(self, request):
        conversation_id = self.get_to_handle_from_request(request)
        conversation = self._conversations[conversation_id]
        return self._requests[conversation.active]

    async def _on_process_completed(self, request, to_handle, send_meta):
        self._record_for_request(request).completed = True

    async def _on_consume_error(self, request, to_handle, err_text):
        self._record_for_request(request).error = err_text
        await self.send(to_handle, err_text)

    async def on_event_message_completed(
        self, request, to_handle, event, send_meta
    ):
        if getattr(event, "type", None) == MessageType.MESSAGE:
            await super().on_event_message_completed(
                request, to_handle, event, send_meta
            )

    async def on_event_content(self, request, to_handle, event, send_meta):
        return False

    async def send(self, to_handle, text, meta=None):
        conversation = self._conversations.get(to_handle)
        if conversation is None or conversation.active is None:
            raise RuntimeError(
                "Page Assist cannot deliver a message "
                "outside an active browser request"
            )
        conversation.messages.append(
            {
                "id": str(uuid4()),
                "requestId": conversation.active,
                "role": "assistant",
                "content": text,
                "status": "complete",
            }
        )

    async def send_approval_notification(
        self,
        *,
        session_id,
        user_id,
        request_id,
        tool_name,
        severity,
        result_summary,
        channel_meta=None,
    ):
        conversation_id = session_id.removeprefix(f"{self.channel}:")
        conversation = self._conversations[conversation_id]
        if conversation.active is None:
            raise RuntimeError(
                "Approval notification has no active browser request"
            )
        self._requests[conversation.active].state = "waiting_approval"
        await self.send(
            conversation_id,
            "Native tool approval is required. Open the QwenPaw Console to "
            f"review it. Tool: {tool_name}; approval ID: {request_id}. "
            "This browser channel never approves tools automatically.",
        )
