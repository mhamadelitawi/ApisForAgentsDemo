"""Auth/scopes, rate limiting and event emission shared by v1 and v2."""

import math
import time
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .problems import Problem
from .state import TOKENS, state

TRACED_PREFIXES = ("/v1/", "/v2/")


class V1Error(Exception):
    """v1 has one error message for everything."""

    def __init__(self, status: int = 400, body: dict | str | None = None):
        self.status = status
        self.body = body if body is not None else {"error": "Invalid request"}

    def response(self) -> Response:
        if isinstance(self.body, str):
            return Response(self.body, status_code=self.status, media_type="text/html")
        return JSONResponse(self.body, status_code=self.status)


@dataclass
class Caller:
    token: str
    label: str
    scopes: set
    max_refund_cents: int | None
    rate_limited: bool
    agent: str | None


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def caller_for(request: Request, version: str) -> Caller:
    token = _bearer(request)
    info = TOKENS.get(token)
    if info is None:
        if version == "v1":
            raise V1Error(400)
        raise Problem("unauthorized", "Send Authorization: Bearer <token>.",
                      next_action="Ask the operator for a valid ShopCo token.")
    caller = Caller(token=token, label=info["label"], scopes=info["scopes"],
                    max_refund_cents=info["max_refund_cents"], rate_limited=info["rate_limited"],
                    agent=request.headers.get("x-agent-label"))
    request.state.caller = caller
    if caller.rate_limited:
        limit = int(state.settings["rate_limit"])
        allowed, remaining, wait = state.limiter.check(token, limit)
        if not allowed:
            state.add_audit(token_label=caller.label, agent=caller.agent, token=token,
                            endpoint=f"{request.method} {request.url.path}", decision="denied",
                            reason=f"rate limited ({limit}/min)")
            if version == "v1":
                raise V1Error(429)
            retry_after = max(1, math.ceil(wait))
            raise Problem("rate-limited", f"Limit is {limit} requests per minute for this token.",
                          next_action=f"Wait {retry_after} seconds, then retry the same request.",
                          headers={"Retry-After": str(retry_after), "RateLimit-Limit": str(limit),
                                   "RateLimit-Remaining": "0", "RateLimit-Reset": str(retry_after)},
                          retry_after_seconds=retry_after)
        request.state.rate_headers = {"RateLimit-Limit": str(limit),
                                      "RateLimit-Remaining": str(remaining)}
    return caller


def require_scope(request: Request, caller: Caller, scope: str, order_id: str | None = None) -> None:
    if scope in caller.scopes:
        return
    state.add_audit(token_label=caller.label, agent=caller.agent, token=caller.token,
                    endpoint=f"{request.method} {request.url.path}", decision="denied",
                    reason=f"missing scope {scope}", order_id=order_id)
    raise Problem("insufficient-scope", f"This token does not have the {scope} scope.",
                  next_action=f"Do not retry. Tell the user this action needs a human with {scope}.",
                  missing_scope=scope, token_scopes=sorted(caller.scopes))


def respond(request: Request, body, status: int = 200, headers: dict | None = None,
            media_type: str | None = None) -> Response:
    request.state.resp_body = body
    hdrs = dict(getattr(request.state, "rate_headers", {}) or {})
    hdrs.update(headers or {})
    return JSONResponse(body, status_code=status, headers=hdrs, media_type=media_type)


async def read_json(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        body = None
    request.state.req_body = body
    return body if isinstance(body, dict) else None


async def trace_requests(request: Request, call_next):
    path = request.url.path
    if not path.startswith(TRACED_PREFIXES):
        return await call_next(request)
    start = time.perf_counter()
    response = await call_next(request)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    caller = getattr(request.state, "caller", None)
    state.bus.emit(
        "http",
        method=request.method,
        path=path,
        query=str(request.url.query or ""),
        status=response.status_code,
        latency_ms=latency_ms,
        api=path.split("/")[1],
        token=caller.label if caller else None,
        agent=request.headers.get("x-agent-label"),
        idempotency_key=request.headers.get("idempotency-key"),
        replayed=response.headers.get("idempotent-replayed") == "true",
        request_body=getattr(request.state, "req_body", None),
        response_body=getattr(request.state, "resp_body", None),
    )
    return response
