# Page Assist channel (0.1.2)

[简体中文](README.zh-CN.md) | English

Repository: [refeved/qwenpaw-channel-page-assist](https://github.com/refeved/qwenpaw-channel-page-assist).
The plugin manifest and entry point sit directly at the repository root;
no QwenPaw source clone or modification is needed. The host comes from the pip release;
the plugin still runs inside the host process.

A standalone, single-Agent channel plugin. It runs in the QwenPaw process with its own
HTTP listen port; it does not modify the core, does not deploy a separate relay service,
and does not call an OpenAI-compatible chat API to bypass the native Agent. It is the
first backend implementation of the Page Assist **Remote Agent** provider. The current
release set is v0.1.2: QwenPaw `2.2.2.post1`, Page Assist `agent-usage-v0.1.2`, this
plugin `qwenpaw-channel-page-assist-v0.1.2` (plugin version `0.1.2`). The UI provider
name is Remote Agent; protocol, storage keys, routes and the frontend client keep the
`qwenpaw` namespace.

## First-version scope

- One channel endpoint binds one Agent in the current workspace; this is not a global
  Agent gateway. Side-panel replies show the bound Agent's configured name (since 0.1.1)
  instead of a fixed label.
- Text questions, follow-ups in the same chat, and new independent chats.
- Explicit sharing of page text or the selection, reusing Page Assist's snapshot preview
  and confirmation.
- The Agent runs through `BaseChannel.consume_one`, the native TaskTracker and the
  workspace execution functions. Native tools, memory and permission policies are not
  replaced.
- Completed text messages are displayed; hidden thinking and tool arguments are not.
  SSE carries whole messages and status only; there is no token-by-token output.
- Native approvals are handled inline in the side panel: the plugin reuses
  ApprovalService's pending records and the native decision entry points (`exact` is
  always available, `similar` only when the native side produced a generalized target).
  Nothing is auto-approved, downgraded or bypassed. Approvals whose ownership cannot be
  established, or that are out of this version's scope, stay in the Console. Pending
  approvals are not written into conversation messages: the button beside the composer
  is grey when nothing is pending, highlights and opens a popover otherwise, and the
  `waiting_approval` state is carried by status and polling only.
- The Remote Agent UI offers text chat, explicit page/selection sharing, image/file/
  page-screenshot attachments (a matching extension build is required; the screenshot is
  captured locally from the visible tab and staged through the same upload path),
  conversation history within the current instance, and new chats; speech dictation, TTS
  playback and prompt insertion run in the browser. Local RAG, web search, MCP, temporary
  chat, per-chat model settings, OCR, Page Action and WebMCP are unavailable or disabled;
  unconnected entries state their reason.
- Firefox does not show the Remote Agent entry (MV2 lacks
  `chrome.storage.local.setAccessLevel`); use Chrome/Chromium or Edge.

Deferred: global multi-Agent discovery, history recovery across restarts, automatic
reconnect after drops, a persistent request ledger, a cancel button, rich media,
proactive push, and approval event push. Do not treat this version as a production
remote gateway.

## Installation and connection

Install this repository root in a terminal that already has a complete QwenPaw runtime:

```bash
qwenpaw plugin install /path/to/qwenpaw-channel-page-assist
```

If the host is not installed yet, run `pip install qwenpaw==2.2.2.post1` first.
After changing plugin code, run
`qwenpaw plugin install /path/to/qwenpaw-channel-page-assist --force` and restart the
host; the install directory is a host-managed copy and does not track repository edits.
Building the QwenPaw Console is not required, and no `PYTHONPATH` setup is needed.

Two installation prerequisites (verified on QwenPaw 2.2.2.post1):

- The host installs `requirements.txt` with pip from its own Python environment; that
  pip must be at least `26.2.2`, or the install is judged unsafe at the dependency step
  and rolls back entirely.
- The source directory must not contain `.git`: the host copies whole directories, and
  read-only git objects on Windows fail the copy. Copy `plugin.json`, `plugin.py`,
  `channel.py`, `protocol.py` and `requirements.txt` into an empty directory first.

In the Console, confirm the plugin is enabled, select the target Agent, and configure
**Page Assist** in channel management:

| Setting | Value |
| --- | --- |
| Enabled | on |
| Browser token | a freshly generated random dedicated secret, at least 32 ASCII characters, no whitespace |
| HTTP bind address | `127.0.0.1` for local verification |
| HTTP port | `8766`; different Agents must use different ports |

Generate a random key in QwenPaw's Python environment with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Never write the key into repositories, logs or chats.

Channel configuration is read only from the **Agent-level** config, i.e.
`channels.page_assist` inside `workspaces/<agent>/agent.json`; the same key in the host
global `config.json` starts no listener. Save it via the Console or the channel
management UI; no manual global Agent routing edits are needed.

After a fresh install or a channel config change, restart QwenPaw and check the log for
`Page Assist channel listening on 127.0.0.1:8766`.
This version does not rely on a "start the new listener, then close the old one" hot
replacement; port conflicts fail loudly.

The steps below require a Page Assist build containing the Remote Agent provider
(release tag `agent-usage-v0.1.2` or the `feat/qwenpaw-remote-channel` branch).
After rebuilding and reloading the extension:

1. In the Chrome side panel header, left of the Page Assist wordmark, switch
   **Local models** to **Remote Agent**. On first use with no provider record / Ollama
   URL, the empty state asks "Choose how you want to chat"; picking Remote Agent without
   a configuration expands the connection form inline.
2. With an existing config, the same form opens from the header switcher or the
   dedicated **Remote Agent** tab (`/settings/qwenpaw`) in the options page / side-panel
   settings. That tab sits beside General Settings, OpenAI Compatible API and Manage
   Models, not inside General Settings.
3. Service URL: `http://127.0.0.1:8766` — not the Console's port.
4. Browser credential: the same Browser token; do not use the Console admin token.
5. Leave the Console address empty for now. The first version keeps the frontend
   same-origin link restriction, and the Console usually runs on a different port, so
   open the original Console manually rather than authorizing through links in chat.
6. Test the connection, save, return. The first question creates a chat automatically;
   the top New button starts another one.

![The Remote Agent tab in the side-panel settings](docs/assets/page-assist-remote-agent-settings-tab.png)

The image shows the side-panel settings tab bar; the options page navigation lists
extra entries such as RAG, Ollama and MCP.

The remote path needs no Ollama configuration. Firefox does not expose the Remote Agent
entry; for UI details and the latest behaviour see the Remote Agent guide in the
Page Assist fork:
[docs/qwenpaw-remote.md](https://github.com/refeved/page-assist-agent-usage/blob/feat/qwenpaw-remote-channel/docs/qwenpaw-remote.md).

When WSL Chrome and QwenPaw share one WSL environment, the loopback addresses above work
directly. Never expose the plaintext listener to the public internet. Remote use
requires your own HTTPS reverse proxy in front of this dedicated port only, keeping the
plugin's Bearer auth; the extension rejects non-loopback HTTP.

**Credential authority**: this version has one shared credential acting for one browser
user, able to invoke the bound Agent's native capabilities. It is not a multi-user
identity system and adds no per-tool token scopes. Restrict it to trusted browsers;
native tool approvals still follow the Agent's own policy.

## State and data boundaries

The browser path stays on `/api/page-assist`. It serves capabilities, the single Agent,
new chats, message submission, in-chat history, request status and request events.
Payloads follow frontend v1 and declare `recovery=false`, `approvals=true`,
`approvalEvents=false`, `attachments=true` (5 MB per attachment, at most 4 per message);
attachments upload as base64 via `POST /attachments` and land in the bound Agent
workspace's `media/page_assist` directory, referenced once per message by attachmentId,
with the Agent receiving `file://` image/file content. The pending list and decisions go
through `GET /approvals` and `POST /approvals/{id}/decision`; the frontend reconciles by
polling every 5 seconds and does not rely on push.

- Request-body gate: the channel caps the whole HTTP body at 8 MiB (a 5 MiB attachment
  inflates to roughly 4/3 in base64). Behind an HTTPS reverse proxy or relay, the front
  layer must allow bodies of at least ~7 MiB (e.g. nginx `client_max_body_size 10m;`),
  or the relay returns its own 413 before the channel ever sees the request. The
  extension compresses and retries an image rejected with 413 once, but deployments
  should still provision enough headroom.
- One request at a time per chat; a busy chat returns 409 instead of joining a message
  merge queue.
- The same requestId with the same payload never re-runs within one instance; differing
  payload or chat returns 409.
- At most 100 chats and 500 requests live at once; capacity limits return 429, nothing is
  evicted silently.
- The pending list exposes only records owned by this channel instance and this browser
  credential's sessions; approvals from other channels or instances are neither visible
  nor decidable. Re-submitting the same `decisionId` returns the confirmed result; a
  decision conflicting with it returns 409 and never overwrites the native conclusion.
- Requests, page payloads and the browser display history live in memory only. After the
  channel is re-instantiated the instanceId changes, frontend caches re-partition
  automatically; you must start a new chat — old browser chats cannot be restored.
- Native history, memory or tool side effects may already be persisted and are **not
  deleted when the channel's memory clears**.
- Closing the browser or hiding the view does not cancel the Agent. After a lost
  submission result or a runtime drop, the frontend never auto-resends and promises no
  recovery; verify in the Console and avoid re-executing requests with side effects.
- Closing the channel stops its native running tasks. Stopping the service makes no
  guarantee that already-executed tool side effects are reversible.

## Development verification

After cloning this repository alone, in a development virtual environment:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests/test_channel.py
```

To verify inside the integration workspace root, reusing the released environment:

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q \
  qwenpaw-channel-page-assist/tests/test_channel.py
cd page-assist
bun run compile
bun run build:chrome
node tests/qwenpaw-channel-browser.mjs
```

Tests also need `pytest`, `pytest-asyncio` and the host's native dependencies such as
AgentScope. The browser test uses the workspace `.venv/bin/python` by default; point
`QWENPAW_TEST_PYTHON` at another interpreter, and `QWENPAW_CHANNEL_DIR` at the repository
root when the plugin lives elsewhere. Tests inject no QwenPaw source paths and use the
host actually installed in that interpreter. System Chrome / WSLg is used, and test
processes and temporary browser directories are cleaned up automatically.

This test set exercises real plugin HTTP, BaseChannel and TaskTracker, while the Agent
execution function, ChatManager and tool discovery are isolated deterministic test
doubles — **not an end-to-end acceptance of model, memory and real approvals**. Final
verification still means manual runs against an existing Agent with the real model and
tools.

## License and disclaimer

This repository is under the [Apache License 2.0](LICENSE).

This project is an independent community plugin, unaffiliated with and unendorsed by the
QwenPaw or Page Assist projects. The plugin runs through QwenPaw's public plugin API and
contains no upstream source code. Page Assist is Muhammed Nazeem's MIT-licensed project
and QwenPaw is an Apache-2.0 project; related names and icons belong to their respective
owners.
