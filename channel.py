"""Single-Agent HTTP transport over QwenPaw's native channel pipeline."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from uuid import UUID, uuid4

from aiohttp import web
from qwenpaw.app.approvals import (
    ApprovalActor,
    ApprovalIdentityMismatchError,
    get_approval_service,
)
from qwenpaw.app.approvals.display import approval_display_fields
from qwenpaw.app.channels.base import BaseChannel
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.config.config import load_agent_config_async
from qwenpaw.constant import DEFAULT_MEDIA_DIR
from qwenpaw.schemas import (
    ContentType,
    FileContent,
    ImageContent,
    MessageType,
    TextContent,
)
from qwenpaw.security.tool_guard.approval import (
    ApprovalDecision,
    ApprovalScope,
)

from .protocol import (
    MAX_ATTACHMENTS,
    MAX_ATTACHMENT_BYTES,
    MAX_CONTEXT,
    MAX_OUTGOING_ATTACHMENTS,
    MAX_OUTGOING_FILE_BYTES,
    MAX_OUTGOING_TOTAL_BYTES,
    MAX_TEXT,
    ApprovalDecisionPayload,
    AttachmentUpload,
    MessagePayload,
    NewConversation,
)

logger = logging.getLogger(__name__)
PREFIX = "/api/page-assist"
TERMINAL = {"completed", "failed", "cancelled"}
APPROVAL_TARGET_MAX = 2000
DECISIONS_MAX = 200
PENDING_ATTACHMENTS_MAX = 32
OUTGOING_MEDIA_MAX = 32
MAX_UPLOAD_BODY = 8 * 1024 * 1024


def read_channel_version() -> str:
    """Version this plugin ships with, read from its own manifest."""
    try:
        manifest = json.loads(
            Path(__file__).with_name("plugin.json").read_text("utf-8")
        )
        return str(manifest["version"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def read_host_version() -> str:
    """QwenPaw's own version, or empty when the host offers no such label."""
    try:
        from qwenpaw.__version__ import __version__
    except ImportError:
        return ""
    return str(__version__).strip()


# Neither of these is negotiated: they are read once per process because the
# manifest cannot change while the channel is running.
CHANNEL_VERSION = read_channel_version()
HOST_VERSION = read_host_version()

IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
)

# A file the Agent hands over rarely rides on the final answer: `send_file`
# returns it as a tool *output*, and the renderer keeps those media parts even
# with `show_tool_results=False` (only the text label is hidden). Forwarding
# `MESSAGE` alone therefore threw away every reply attachment, while tool
# *calls*, reasoning and approval events still carry nothing to display.
FORWARDED_MESSAGE_TYPES = (
    MessageType.MESSAGE,
    MessageType.FUNCTION_CALL_OUTPUT,
    MessageType.PLUGIN_CALL_OUTPUT,
    MessageType.MCP_TOOL_CALL_OUTPUT,
)


def classify_image(raw: bytes) -> tuple[str | None, str | None]:
    """Return (extension, media type) when magic bytes prove a real image."""
    for prefix, extension, media_type in IMAGE_MAGIC:
        if raw.startswith(prefix):
            return extension, media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None, None


def sanitize_name(name: str) -> str:
    """Keep a displayable basename; path parts and control bytes are dropped."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(".")
    return cleaned[:120] or "attachment"


def safe_suffix(name: str) -> str:
    suffix = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix or ""):
        return suffix.lower()
    return ""


DATA_URL_RE = re.compile(
    r"^data:(?P<mt>[\w.+-]+/[\w.+-]+)?;base64,(?P<data>.*)$",
    re.DOTALL,
)
# Only bytes that stay inside the media directory are ever written to disk;
# anything else becomes a file so the browser never receives a raw type.
INLINE_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
}
MEDIA_TYPE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml"
    ".document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ".sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml"
    ".presentation",
    ".zip": "application/zip",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def media_type_for(kind: str, name: str, declared: str | None) -> str:
    """Pick a Content-Type the browser can render or hand to a downloader."""
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    guessed = MEDIA_TYPE_BY_SUFFIX.get(safe_suffix(name))
    if guessed:
        return guessed
    if kind == "image":
        return "image/png"
    return "application/octet-stream"


def media_part_reference(part) -> tuple[str, str, str, str] | None:
    """Return (kind, payload, name, declared type) for an outgoing media part.

    Payload is a data URL, an absolute local URI, or a remote HTTP(S) URL;
    audio only ever reaches this point as raw base64, which is treated as a
    data URL without a declared media type.
    """
    kind = getattr(part, "type", None)
    name = ""
    if kind == ContentType.IMAGE and getattr(part, "image_url", None):
        payload = part.image_url
    elif kind == ContentType.VIDEO and getattr(part, "video_url", None):
        payload, name = part.video_url, "video.mp4"
    elif kind == ContentType.AUDIO and getattr(part, "data", None):
        payload, name = part.data, "audio"
    elif kind == ContentType.FILE:
        payload = getattr(part, "file_url", None) or ""
        name = getattr(part, "filename", None) or ""
    else:
        return None
    if not isinstance(payload, str) or not payload.strip():
        return None
    kind = "image" if kind == ContentType.IMAGE else "file"
    return kind, payload, name, None


def media_payload_source(payload: str) -> tuple[str, Any]:
    """Classify a media payload as data, file, http, or unsupported."""
    match = DATA_URL_RE.match(payload.strip())
    if match:
        return "data", match.groupdict()
    if payload.startswith("data:"):
        # Truncated or unparsable data URL: never ship it to the browser.
        return "unsupported", None
    try:
        url = urlsplit(payload)
    except ValueError:
        return "unsupported", None
    if url.scheme == "file":
        return "file", url
    if url.scheme in {"http", "https"} and url.netloc:
        return "http", payload
    return "unsupported", None


def suggested_name(payload: str, source: str, value: Any) -> str:
    """Best readable basename for media whose part carries no filename."""
    if source == "http":
        value = urlsplit(payload)
        tail = unquote(value.path.rsplit("/", 1)[-1])
    elif source == "file":
        tail = unquote(value.path.rsplit("/", 1)[-1])
    elif source == "data":
        extension = (value.get("mt") or "").split("/")[-1].split("+")[0]
        tail = f"image.{extension}" if extension else ""
    else:
        tail = ""
    return sanitize_name(tail)


@dataclass
class Conversation:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    active: str | None = None


@dataclass
class AttachmentRecord:
    id: str
    name: str
    kind: str
    media_type: str
    bytes: int
    path: Path
    used_by: str | None = None


@dataclass
class OutgoingMedia:
    """Bytes this channel will serve back to the browser on demand."""

    id: str
    name: str
    kind: str
    media_type: str
    bytes: int
    path: Path


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
        self._attachments: dict[str, AttachmentRecord] = {}
        self._outgoing_media: dict[str, OutgoingMedia] = {}
        self._decisions: dict[str, dict[str, Any]] = {}
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
            client_max_size=MAX_UPLOAD_BODY,
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
                web.get(f"{PREFIX}/approvals", self._approvals),
                web.post(
                    f"{PREFIX}/approvals/{{id}}/decision", self._decide
                ),
                web.post(f"{PREFIX}/attachments", self._upload_attachment),
                web.get(f"{PREFIX}/media/{{id}}", self._media),
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
        payload = {
            "protocolVersion": 1,
            "instanceId": self._instance,
            "principalScope": self._user,
            "maxContextChars": MAX_CONTEXT,
            "maxTextChars": MAX_TEXT,
            "recovery": False,
            "approvals": True,
            "approvalEvents": False,
            "attachments": True,
            "maxAttachmentBytes": MAX_ATTACHMENT_BYTES,
            "maxAttachments": MAX_ATTACHMENTS,
            "media": True,
        }
        # The versions are an extra for the browser settings panel, so that a
        # user can tell which deployment they are talking to. Nothing may be
        # made mandatory by them: an older browser ignores the fields, an older
        # channel sends neither, and a build that cannot read them omits them
        # instead of guessing. `protocolVersion` therefore stays 1.
        if CHANNEL_VERSION:
            payload["channelVersion"] = CHANNEL_VERSION
        if HOST_VERSION:
            payload["hostVersion"] = HOST_VERSION
        return web.json_response(payload)

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
        attachments = self._claim_attachments(payload)
        record = Request(payload, conversation)
        self._requests[payload.requestId] = record
        conversation.active = payload.requestId
        conversation.messages.append(
            {
                "id": f"{payload.requestId}:user",
                "requestId": payload.requestId,
                "role": "user",
                "content": self._user_message_text(payload, attachments),
                "attachments": self._user_message_attachments(attachments),
                "status": "complete",
            }
        )
        record.task = asyncio.create_task(self._execute(record))
        return web.json_response(record.dto(), status=202)

    def _claim_attachments(
        self, payload: MessagePayload
    ) -> list[AttachmentRecord]:
        """Mark every referenced upload as used by this requestId, once.

        Checks and claims run without an await in between, so two concurrent
        submits can never consume the same attachment. Any later failure of
        the request leaves the claim dropped (the record simply expires with
        the channel), which is safe: the browser re-uploads on retry.
        """
        claimed = []
        for ref in payload.attachments or []:
            attachment = self._attachments.get(ref.attachmentId)
            if (
                attachment is None
                or attachment.used_by is not None
                or not attachment.path.is_file()
            ):
                for record in claimed:
                    record.used_by = None
                raise web.HTTPBadRequest(
                    text="Attachment unavailable; upload it again"
                )
            claimed.append(attachment)
        for attachment in claimed:
            attachment.used_by = payload.requestId
        return claimed

    @staticmethod
    def _user_message_text(payload, attachments) -> str:
        """History line for the browser; media itself lives on disk."""
        if not attachments:
            return payload.text
        notes = "\n".join(
            f"[attachment {a.kind}: {a.name}]" for a in attachments
        )
        return (
            f"{payload.text}\n{notes}" if payload.text.strip() else notes
        )

    @staticmethod
    def _user_message_attachments(attachments) -> list[dict[str, Any]]:
        """Media references for the browser, next to the textual history line.

        Uploads already live on disk, so the same protected download endpoint
        that serves Agent media can serve them back: the user sees the real
        screenshot instead of only a filename.
        """
        return [
            {
                "mediaId": record.id,
                "kind": record.kind,
                "name": record.name,
                "mediaType": record.media_type,
                "bytes": record.bytes,
            }
            for record in attachments
        ]

    def _media_dir(self) -> Path:
        """Workspace media dir, mirroring the official channels."""
        workspace_dir = (
            getattr(self._workspace, "workspace_dir", None)
            if self._workspace
            else None
        )
        base = (
            Path(workspace_dir) / "media" / "page_assist"
            if workspace_dir
            else DEFAULT_MEDIA_DIR / "page_assist"
        )
        base.mkdir(parents=True, exist_ok=True)
        return base

    async def _upload_attachment(self, request):
        body = await self._body(request, AttachmentUpload)
        if len(self._attachments) >= PENDING_ATTACHMENTS_MAX:
            raise web.HTTPTooManyRequests(
                text="Too many unattached uploads; send or restart the channel"
            )
        raw = body.raw()
        image_ext, image_mime = classify_image(raw)
        name = sanitize_name(body.name)
        suffix = safe_suffix(name) or (image_ext or "")
        attachment_id = str(uuid4())
        path = self._media_dir() / f"{attachment_id}{suffix}"
        path.write_bytes(raw)
        media_type = image_mime or body.mediaType or "application/octet-stream"
        self._attachments[attachment_id] = AttachmentRecord(
            id=attachment_id,
            name=name,
            kind="image" if image_ext else "file",
            media_type=media_type,
            bytes=len(raw),
            path=path,
        )
        # Register the same bytes in the media index so one protected endpoint
        # serves both directions and the file shares the outgoing capacity cap.
        self._outgoing_media[attachment_id] = OutgoingMedia(
            id=attachment_id,
            name=name,
            kind=self._attachments[attachment_id].kind,
            media_type=media_type,
            bytes=len(raw),
            path=path,
        )
        return web.json_response(
            {
                "attachmentId": attachment_id,
                "name": name,
                "kind": self._attachments[attachment_id].kind,
                "mediaType": media_type,
                "bytes": len(raw),
            },
            status=201,
        )

    def _outgoing_bytes(self) -> int:
        return sum(record.bytes for record in self._outgoing_media.values())

    def _claim_outgoing_bytes(self, needed: int) -> None:
        """Free space for *needed* bytes, oldest media first."""
        for record in sorted(
            self._outgoing_media.values(),
            key=lambda item: item.id,
        ):
            if self._outgoing_bytes() + needed <= MAX_OUTGOING_TOTAL_BYTES:
                return
            self._outgoing_media.pop(record.id, None)
            record.path.unlink(missing_ok=True)

    def _store_outgoing(
        self,
        payload_bytes: bytes,
        kind: str,
        name: str,
        declared: str | None,
    ) -> dict[str, Any]:
        """Persist reply media under a fresh id and return its reference."""
        if not payload_bytes:
            return {"skip": "Media is empty and was skipped."}
        if len(payload_bytes) > MAX_OUTGOING_FILE_BYTES:
            limit = MAX_OUTGOING_FILE_BYTES // (1024 * 1024)
            return {
                "skip": (
                    f"Media '{name}' is larger than {limit} MB "
                    "and was skipped."
                )
            }
        self._claim_outgoing_bytes(len(payload_bytes))
        media_id = str(uuid4())
        image_ext, image_mime = classify_image(payload_bytes)
        extension = image_ext or safe_suffix(name)
        path = self._media_dir() / f"{media_id}{extension}"
        path.write_bytes(payload_bytes)
        media_type = media_type_for(
            kind,
            name,
            image_mime or declared,
        )
        self._outgoing_media[media_id] = OutgoingMedia(
            id=media_id,
            name=name,
            kind=kind,
            media_type=media_type,
            bytes=len(payload_bytes),
            path=path,
        )
        return {
            "media": {
                "mediaId": media_id,
                "kind": kind,
                "name": name,
                "mediaType": media_type,
                "bytes": len(payload_bytes),
            }
        }

    def _content_parts_for_send(self, parts):
        """Split outgoing parts into text, media references and skip notices.

        A local file the Agent produced is copied into the media directory, so
        no absolute server path ever reaches the browser; a public HTTP(S) URL
        is forwarded untouched because fetching it server-side would turn this
        channel into an open proxy.
        """
        texts, media, notices = [], [], []
        for part in parts:
            ptype = getattr(part, "type", None)
            if ptype == ContentType.TEXT and getattr(part, "text", None):
                texts.append(part.text)
                continue
            if ptype == ContentType.REFUSAL and getattr(part, "refusal", None):
                texts.append(part.refusal)
                continue
            reference = media_part_reference(part)
            if reference is None:
                continue
            kind, payload, name, declared = reference
            source, value = media_payload_source(payload)
            if source == "unsupported":
                notices.append(
                    f"[{kind}: this attachment format is not "
                    "supported by the browser channel]"
                )
                continue
            name = sanitize_name(
                name or suggested_name(payload, source, value)
            )
            if len(media) >= MAX_OUTGOING_ATTACHMENTS:
                notices.append(
                    f"[{kind}: {name} skipped, more than "
                    f"{MAX_OUTGOING_ATTACHMENTS} attachments]"
                )
                continue
            if source == "http":
                media.append(
                    {
                        "url": value,
                        "kind": kind,
                        "name": name,
                        "mediaType": media_type_for(kind, name, declared),
                        "bytes": 0,
                    }
                )
                continue
            payload_bytes = b""
            if source == "data":
                try:
                    payload_bytes = base64.b64decode(
                        value["data"],
                        validate=True,
                    )
                except (binascii.Error, ValueError):
                    notices.append(
                        f"[{kind}: {name} could not be decoded]"
                    )
                    continue
                declared = declared or value.get("mt")
            else:
                local = Path(url2pathname(value.path))
                if not local.is_file():
                    notices.append(
                        f"[{kind}: {name} is no longer available]"
                    )
                    continue
                payload_bytes = local.read_bytes()
            outcome = self._store_outgoing(
                payload_bytes,
                kind,
                name,
                declared,
            )
            if "skip" in outcome:
                notices.append(f"[{outcome['skip']}]")
            else:
                media.append(outcome["media"])
        return texts, media, notices

    async def _media(self, request):
        """Serve reply and upload bytes; the bearer token gates this route."""
        try:
            media_id = str(UUID(request.match_info["id"]))
        except ValueError:
            raise web.HTTPNotFound(text="Media unavailable") from None
        record = self._outgoing_media.get(media_id)
        if record is None or not record.path.is_file():
            raise web.HTTPNotFound(text="Media unavailable")
        inline = (
            record.kind == "image"
            and record.media_type in INLINE_MEDIA_TYPES
        )
        return web.FileResponse(
            record.path,
            headers={
                "Content-Type": record.media_type,
                "Content-Disposition": (
                    f'inline; filename="{record.name}"'
                    if inline
                    else f'attachment; filename="{record.name}"'
                ),
            },
        )

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
                            "attachments": message.get("attachments") or [],
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

    def _approval_conversation(self, pending):
        """Return the browser conversation owning *pending*, if any.

        Only approvals raised by this channel instance and this browser
        credential are visible; anything else stays with its own surface.
        """
        if pending.channel != self.channel or pending.user_id != self._user:
            return None
        prefix = f"{self.channel}:"
        for session_id in (pending.session_id, pending.root_session_id):
            if not session_id.startswith(prefix):
                continue
            conversation = self._conversations.get(session_id[len(prefix) :])
            if conversation is not None:
                return conversation
        return None

    @staticmethod
    def _approval_target(pending, display):
        """Show the native target; never a model-written summary."""
        target = str(display.get("exact_target") or "").strip()
        if target:
            return target[:APPROVAL_TARGET_MAX]
        tool_call = pending.extra.get("tool_call")
        params = None
        if isinstance(tool_call, dict):
            params = tool_call.get("input")
        if isinstance(params, dict) and params:
            detail = json.dumps(params, ensure_ascii=False, sort_keys=True)
            return f"{pending.tool_name} {detail}"[:APPROVAL_TARGET_MAX]
        return pending.tool_name or "Tool"

    def _approval_dto(self, pending):
        conversation = self._approval_conversation(pending)
        if conversation is None or conversation.active is None:
            return None
        display = approval_display_fields(pending)
        expires_at = pending.created_at + pending.timeout_seconds
        # The side panel renders "Approve — {label}", so labels stay short
        # and only name the scope, matching Console wording.
        scopes = [
            {
                "id": ApprovalScope.EXACT.value,
                "label": "Exact target",
                "description": "Run this tool call once with the target shown "
                "above. Native policy may remember the literal target, so "
                "this is not a one-off promise.",
            }
        ]
        if display.get("is_generalized"):
            scopes.append(
                {
                    "id": ApprovalScope.SIMILAR.value,
                    "label": "Similar pattern",
                    "description": "Allow this and later calls matching the "
                    "generalized target.",
                }
            )
        risk = pending.result_summary.strip() or f"Severity {pending.severity}"
        return {
            "id": pending.request_id,
            "conversationId": conversation.id,
            "agentId": pending.agent_id,
            "requestId": conversation.active,
            "tool": display.get("tool_display_name") or pending.tool_name,
            "target": self._approval_target(pending, display),
            "risk": risk,
            "expiresAt": datetime.fromtimestamp(
                expires_at,
                timezone.utc,
            ).isoformat(),
            "actionable": (
                pending.status == "pending" and time.time() < expires_at
            ),
            "scopes": scopes,
        }

    async def _approvals(self, request):
        service = get_approval_service()
        # pylint: disable=protected-access
        async with service._lock:
            pending = list(service._pending.values())
        approvals = [
            dto
            for dto in (self._approval_dto(item) for item in pending)
            if dto is not None
        ]
        return web.json_response({"approvals": approvals})

    async def _decide(self, request):
        approval_id = request.match_info["id"]
        payload = await self._body(request, ApprovalDecisionPayload)
        previous = self._decisions.get(approval_id)
        if previous is not None:
            if previous["payload"] != payload.model_dump():
                raise web.HTTPConflict(
                    text="A different decision was already confirmed"
                )
            return web.json_response(previous["response"])

        service = get_approval_service()
        pending = await service.get_request(approval_id)
        if pending is None:
            raise web.HTTPNotFound(text="Approval is no longer pending")
        conversation = self._approval_conversation(pending)
        if conversation is None:
            raise web.HTTPNotFound(
                text="Approval does not belong to this browser channel"
            )
        expires_at = pending.created_at + pending.timeout_seconds
        if pending.status != "pending" or time.time() >= expires_at:
            raise web.HTTPConflict(text="Approval already resolved or expired")

        scope = None
        if payload.approved:
            try:
                scope = ApprovalScope(
                    payload.scope or ApprovalScope.EXACT.value
                )
            except ValueError:
                raise web.HTTPBadRequest(
                    text="Unsupported approval scope"
                ) from None
            if (
                scope is ApprovalScope.SIMILAR
                and not approval_display_fields(pending).get("is_generalized")
            ):
                raise web.HTTPBadRequest(
                    text="This approval has no generalized scope"
                )

        # This instance only serves the sessions owned by its browser
        # credential, so the actor mirrors the native request instead of
        # claiming Console administrator rights.
        actor = ApprovalActor(
            session_id=pending.session_id,
            root_session_id=pending.root_session_id,
            user_id=pending.user_id,
            channel=pending.channel,
            agent_id=pending.agent_id,
        )
        try:
            resolved = await service.resolve_request(
                approval_id,
                ApprovalDecision.APPROVED
                if payload.approved
                else ApprovalDecision.DENIED,
                scope=scope,
                actor=actor,
            )
        except ApprovalIdentityMismatchError:
            raise web.HTTPForbidden(
                text="Approval caller does not own this request"
            ) from None
        if resolved is None:
            raise web.HTTPConflict(text="Approval already resolved or expired")

        response = {
            "approvalId": approval_id,
            "decisionId": payload.decisionId,
            "approved": payload.approved,
            "status": "confirmed",
        }
        if scope is not None:
            response["scope"] = scope.value
        self._decisions[approval_id] = {
            "payload": payload.model_dump(),
            "response": response,
        }
        while len(self._decisions) > DECISIONS_MAX:
            self._decisions.pop(next(iter(self._decisions)))
        if conversation.active is not None:
            record = self._requests.get(conversation.active)
            if record is not None and record.state == "waiting_approval":
                record.state = "running"
        logger.info(
            "Page Assist approval %s resolved: approved=%s scope=%s",
            approval_id[:8],
            payload.approved,
            scope.value if scope else "none",
        )
        return web.json_response(response)

    def get_to_handle_from_request(self, request):
        return request.session_id.removeprefix(f"{self.channel}:")

    def get_on_reply_sent_args(self, request, to_handle):
        return request.user_id, request.session_id

    def _content_parts(self, record):
        """Agent content list: text first, uploads as local file URIs."""
        base = record.payload.agent_text()
        if not base.strip() and record.payload.attachments:
            base = "The user sent files without a question."
        parts = [TextContent(text=base)]
        for ref in record.payload.attachments or []:
            attachment = self._attachments.get(ref.attachmentId)
            if attachment is None:  # channel restarted; text still runs
                continue
            uri = attachment.path.resolve().as_uri()
            if attachment.kind == "image":
                parts.append(
                    ImageContent(type=ContentType.IMAGE, image_url=uri)
                )
            else:
                parts.append(
                    FileContent(
                        type=ContentType.FILE,
                        file_url=uri,
                        filename=attachment.name,
                    )
                )
        return parts

    async def _execute(self, record):
        record.state = "running"
        try:
            request = self.build_agent_request_from_user_content(
                self.channel,
                self._user,
                f"{self.channel}:{record.conversation.id}",
                self._content_parts(record),
            )
            request.channel_meta = {"conversation_id": record.conversation.id}
            request.channel_instance = self
            chat = await self._workspace.chat_manager.get_or_create_chat(
                request.session_id,
                self._user,
                self.channel,
                name=record.payload.text.strip() or "Shared files",
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
        """Forward the answer plus any media a tool produced.

        Tool outputs are included because that is where `send_file` puts real
        media; their text label stays hidden by `show_tool_results=False`, so
        only the attachment reaches the panel. Everything else (reasoning, tool
        calls, approval events with no `type`) keeps being dropped.
        """
        if getattr(event, "type", None) in FORWARDED_MESSAGE_TYPES:
            await super().on_event_message_completed(
                request, to_handle, event, send_meta
            )

    async def on_event_content(self, request, to_handle, event, send_meta):
        return False

    async def send_content_parts(self, to_handle, parts, meta=None):
        """Send text plus real media references; never a server-side path.

        The default implementation would flatten every image and file into a
        `[Image: file:///...]` line, which leaks a local path and stays
        unusable in the side panel, and its `send_media` hook is a no-op.
        """
        texts, media, notices = self._content_parts_for_send(parts)
        body = "\n".join(texts).strip()
        prefix = (meta or {}).get("bot_prefix", "") or ""
        if prefix and body:
            body = prefix + "  " + body
        if notices:
            notice_text = "\n".join(notices)
            body = f"{body}\n{notice_text}" if body else notice_text
        if body or media:
            await self.send(to_handle, body, meta, attachments=media)

    async def send(self, to_handle, text, meta=None, attachments=()):
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
                "attachments": [dict(item) for item in attachments],
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
        """Mark the request as pending; the panel polls the list.

        No chat message is appended: the composer approval button is the
        single surface, so the transcript holds only real conversation.
        """
        conversation_id = session_id.removeprefix(f"{self.channel}:")
        conversation = self._conversations.get(conversation_id)
        record = None
        if conversation is not None and conversation.active is not None:
            record = self._requests.get(conversation.active)
        if record is None:
            logger.warning(
                "Page Assist approval %s has no active browser request",
                request_id,
            )
            return
        record.state = "waiting_approval"
        logger.info(
            "Page Assist approval %s pending for %s (severity %s): %s",
            request_id[:8],
            tool_name,
            severity,
            result_summary.strip()[:120] or "no summary",
        )
