"""Shared plumbing for the ShopCo MCP servers: HTTP app, events, client labels."""

import asyncio
import contextlib
import json
import os

import httpx
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

API_URL = os.getenv("SHOPCO_API_URL", "http://localhost:8000").rstrip("/")
TOKEN_SUPPORT_AGENT = os.getenv("TOKEN_SUPPORT_AGENT", "demo-agent-token")
TOKEN_ADMIN = os.getenv("TOKEN_ADMIN", "demo-admin-token")

http = httpx.AsyncClient(base_url=API_URL, timeout=15)


async def emit(type_: str, **data) -> None:
    """Push an event to the dashboard. Never let tracing break a tool call."""
    try:
        await http.post("/admin/events", json={"type": type_, **data},
                        headers={"Authorization": f"Bearer {TOKEN_ADMIN}"}, timeout=3)
    except Exception:
        pass


async def settings() -> dict:
    r = await http.get("/admin/settings", timeout=5)
    r.raise_for_status()
    return r.json()


def client_label(server: Server) -> str:
    """Name the connected agent from the MCP initialize handshake (e.g. claude-code, gemini-cli)."""
    try:
        info = server.request_context.session.client_params.clientInfo
        return f"{info.name}" if info else "unknown-client"
    except Exception:
        return "unknown-client"


def session_key(server: Server) -> str:
    try:
        return str(id(server.request_context.session))
    except Exception:
        return "no-session"


def tool_dump(tools: list[types.Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]


def http_text(resp: httpx.Response) -> str:
    """What the agent reads: the status line, then the API body exactly as the API wrote it."""
    line = f"HTTP {resp.status_code} {resp.reason_phrase}"
    if resp.headers.get("idempotent-replayed") == "true":
        line += " (Idempotent-Replayed: true)"
    if resp.headers.get("retry-after"):
        line += f" (Retry-After: {resp.headers['retry-after']})"
    body = resp.text
    try:
        body = json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except Exception:
        pass
    return f"{line}\n{body}"


class NotifyingServer(Server):
    """A server whose tool list depends on demo settings and that tells clients when it changes.

    Each session remembers which tool set ("toolset key") it last listed; when the key computed
    from the settings changes, those sessions get notifications/tools/list_changed.
    """

    def __init__(self, name: str, toolset_key):
        super().__init__(name)
        self.toolset_key = toolset_key          # settings dict -> hashable key
        self.sessions: dict[str, dict] = {}     # session key -> {"session", "key", "agent"}

    def create_initialization_options(self, notification_options=None, experimental_capabilities=None):
        # Advertise tools.listChanged so clients refetch tools when we notify them.
        return super().create_initialization_options(NotificationOptions(tools_changed=True), experimental_capabilities)

    def remember(self, key) -> None:
        """Call from list_tools: this session now holds the tool set for `key`."""
        try:
            self.sessions[session_key(self)] = {"session": self.request_context.session, "key": key,
                                                "agent": client_label(self)}
        except LookupError:
            pass

    async def nudge_if_stale(self, key) -> None:
        """Call from call_tool: a client still holding an old tool set gets the notification on this
        request's own stream too, for clients that never open the standalone notification stream."""
        known = self.sessions.get(session_key(self))
        if known and known["key"] != key:
            try:
                await self.request_context.session.send_notification(
                    types.ServerNotification(types.ToolListChangedNotification()),
                    related_request_id=self.request_context.request_id)
            except Exception:
                pass

    async def watch(self, describe) -> None:
        """Poll the demo settings; when the tool set changes, tell every connected client to reload."""
        last = None
        while True:
            try:
                cfg = await settings()
            except Exception:
                await asyncio.sleep(1)
                continue
            key = self.toolset_key(cfg)
            if last is not None and key != last:
                notified = []
                for skey, entry in list(self.sessions.items()):
                    if entry["key"] == key:
                        continue
                    try:
                        await entry["session"].send_tool_list_changed()
                        notified.append(entry["agent"])
                    except Exception:
                        self.sessions.pop(skey, None)  # client went away
                await emit("tools_changed", server=self.name, reason=describe(cfg), notified=notified)
            last = key
            await asyncio.sleep(0.5)


def serve(server: Server, port: int, background=None) -> None:
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    async def health(_request):
        return JSONResponse({"ok": True, "server": server.name})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            task = asyncio.create_task(background()) if background else None
            try:
                yield
            finally:
                if task:
                    task.cancel()

    starlette_app = Starlette(routes=[Route("/health", health)], lifespan=lifespan)

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            await manager.handle_request(scope, receive, send)
        else:
            await starlette_app(scope, receive, send)

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
