"""Demo controls. Localhost-only, no auth (the ports bind to 127.0.0.1)."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent.scenes import BY_ID, SCENES

from .state import TOKEN_ADMIN, state

router = APIRouter(prefix="/admin")

INJECTION_VARIANTS = ("off", "visible", "hidden", "tool-poisoning")


class Settings(BaseModel):
    version: str | None = None
    lose_next_response: bool | None = None
    rate_limit: int | None = None
    injection: str | None = None
    approval_threshold_cents: int | None = None
    refund_velocity_per_customer: int | None = None
    idempotency_fallback: bool | None = None


@router.get("/state")
async def get_state():
    return {
        "settings": state.public_settings(),
        "orders": list(state.orders.values()),
        "ledger": state.ledger,
        "audit": state.audit,
        "approvals": list(state.approvals.values()),
        "emails": state.emails,
    }


@router.get("/settings")
async def get_settings():
    return state.public_settings()


@router.post("/settings")
async def set_settings(s: Settings):
    changes = s.model_dump(exclude_none=True)
    if changes.get("version") not in (None, "v1", "v2"):
        raise HTTPException(400, "version must be v1 or v2")
    if changes.get("injection") not in (None, *INJECTION_VARIANTS):
        raise HTTPException(400, f"injection must be one of {INJECTION_VARIANTS}")
    if "rate_limit" in changes and changes["rate_limit"] < 1:
        raise HTTPException(400, "rate_limit must be >= 1")
    return state.update_settings(**changes)


@router.post("/reset")
async def reset():
    state.reset()
    return {"ok": True, "settings": state.public_settings()}


@router.get("/scenes")
async def scenes():
    return SCENES


@router.post("/scenes/{scene_id}")
async def load_scene(scene_id: str):
    scene = BY_ID.get(scene_id)
    if scene is None:
        raise HTTPException(404, f"unknown scene {scene_id}")
    state.reset(emit=False)
    state.settings["scene"] = scene_id
    state.update_settings(**scene["preset"])
    state.bus.emit("scene", scene=scene, settings=state.public_settings())
    return {"ok": True, "scene": scene, "settings": state.public_settings()}


@router.post("/events")
async def post_event(request: Request):
    """Internal: the MCP servers and the runner push tool-level events here."""
    if request.headers.get("authorization") != f"Bearer {TOKEN_ADMIN}":
        raise HTTPException(401)
    event = await request.json()
    type_ = event.pop("type", "external")
    event.pop("id", None)
    event.pop("ts", None)
    return state.bus.emit(type_, **event)
