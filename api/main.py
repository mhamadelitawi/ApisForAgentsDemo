"""ShopCo Orders API: v1, v2, admin, SSE events and the dashboard."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import admin, v1, v2
from .middleware import V1Error, trace_requests
from .problems import Problem
from .state import state

app = FastAPI(title="ShopCo Orders API (demo)", docs_url="/docs")
app.middleware("http")(trace_requests)
app.include_router(v1.router)
app.include_router(v2.router)
app.include_router(admin.router)

STATIC = Path(__file__).parent / "static"


@app.exception_handler(Problem)
async def problem_handler(request: Request, exc: Problem):
    request.state.resp_body = exc.body
    resp = exc.response()
    for k, v in (getattr(request.state, "rate_headers", None) or {}).items():
        resp.headers.setdefault(k, v)
    return resp


@app.exception_handler(V1Error)
async def v1_handler(request: Request, exc: V1Error):
    request.state.resp_body = exc.body
    return exc.response()


@app.get("/health")
async def health():
    return {"ok": True, "version": state.settings["version"]}


@app.get("/")
async def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/state")
async def state_page():
    return FileResponse(STATIC / "state.html")


@app.get("/events")
async def events(request: Request):
    async def stream():
        q = state.bus.subscribe()
        try:
            # Late joiners get everything since the last reset/scene load.
            history = list(state.bus.history)
            start = max((i for i, e in enumerate(history) if e["type"] in ("reset", "scene")), default=0)
            yield f"event: hello\ndata: {json.dumps({'settings': state.public_settings()})}\n\n"
            for e in history[start:]:
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            state.bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
