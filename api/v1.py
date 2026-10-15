"""v1: the human-grade API. Deliberately the way many real internal APIs look.

Undocumented behavior (the tool description only says "Processes an order."):
- action codes are cryptic: "rf" refund, "cx" cancel, "pc" partial capture. "refund" is rejected.
- amt is in DOLLARS for "rf" and in CENTS for "pc".
- flag must be true to actually execute; with flag=false the call is a silent no-op that still
  answers {"ok": true}.
- refunds are checked against the order total, not what is left, and there is no idempotency.
- every problem answers 400 {"error": "Invalid request"}.
- reads come from a lagging cache: after a refund whose response was lost, GET keeps showing the
  order as it was before the refund for STALE_READ_SECONDS, or until the next refund call on it.
  So an agent that checks before retrying still sees "not refunded" and refunds twice.
"""

import copy
import hashlib
import time

from fastapi import APIRouter, Request

from .middleware import V1Error, caller_for, read_json, respond
from .state import CUSTOMERS, state

router = APIRouter(prefix="/v1")

STALE_READ_SECONDS = 60

STATUS_CODES = {"delivered": "DLV", "paid": "PD", "pending": "PND", "refunded": "RFD", "cancelled": "CXL"}


def _noise(order_id: str) -> dict:
    """About 4 KB of internal fields nobody outside the warehouse team understands."""
    h = hashlib.sha256(order_id.encode()).hexdigest()
    return {
        "wh": {"code": f"BEY-{h[:3].upper()}", "zone": h[3:5], "bin": f"{h[5:7]}-{h[7:9]}",
               "pick_seq": int(h[9:13], 16), "carrier_ref": f"ARX{h[13:25].upper()}",
               "legs": [{"hub": hub, "scan": f"2026-10-0{i + 2}T0{i}:12:00Z", "code": f"S{h[i:i + 4]}"}
                        for i, hub in enumerate(["BEY", "TRP", "JBL", "BEY"])]},
        "pp_raw": {
            "provider": "paycore", "api_version": "2019-03-14",
            "charge": {"id": f"ch_{h[:24]}", "object": "charge", "amount_minor": None, "captured": True,
                       "balance_txn": f"txn_{h[24:48]}", "outcome": {"network_status": "approved_by_network",
                                                                     "risk_level": "normal", "risk_score": int(h[48:50], 16) % 60,
                                                                     "seller_message": "Payment complete.", "type": "authorized"},
                       "card": {"brand": "visa", "last4": str(int(h[50:54], 16) % 10000).zfill(4),
                                "exp_month": 4, "exp_year": 2029, "fingerprint": h[30:46],
                                "checks": {"cvc_check": "pass", "address_line1_check": "unavailable",
                                           "address_postal_code_check": "unavailable"},
                                "three_d_secure": {"authenticated": True, "version": "2.2.0"}},
                       "metadata": {f"k{i}": h[i * 2:i * 2 + 10] for i in range(18)}},
            "events": [{"id": f"evt_{h[i:i + 20]}", "type": t, "pending_webhooks": i % 3}
                       for i, t in enumerate(["charge.succeeded", "charge.captured", "payment_intent.succeeded",
                                              "balance.available", "charge.updated", "invoice.paid"])],
        },
        "legacy": {"erp_id": int(h[:8], 16), "sync": "OK", "flags": "0x" + h[56:64],
                   "tax": {"vat_bp": 1100, "region": "LB-BA", "exempt": False, "lines": [h[i:i + 6] for i in range(0, 60, 6)]},
                   "audit_trail": [f"{h[i:i + 8]}:{h[i + 8:i + 12]}" for i in range(0, 48, 4)]},
        "fx_log": [{"seq": i, "op": op, "usr": f"svc_{h[i:i + 4]}", "ok": 1, "rc": f"E{h[i + 4:i + 7].upper()}",
                    "at": f"2026-10-0{1 + i % 9}T1{i % 10}:0{i % 6}:00Z"}
                   for i, op in enumerate(["ALLOC", "PICK", "PACK", "LBL", "MAN", "HND", "SCN", "OFD", "DLV", "POD",
                                           "RCN", "ARC"])],
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    caller = caller_for(request, "v1")
    order = state.orders.get(order_id)
    if order is None:
        raise V1Error(400)
    snapshot, expires = state.v1_stale.get(order_id, (None, 0.0))
    if snapshot and time.monotonic() < expires:
        order = snapshot
    state.note_read(caller.token, order_id, "v1", caller.agent)
    customer = CUSTOMERS[order["customer_id"]]
    return respond(request, {
        "id": order_id,
        "cid": order["customer_id"],
        "st": STATUS_CODES.get(order["status"], "UNK"),
        "amount": order["total_cents"],
        "amt_ref": order["refunded_cents"],
        "cur": 840,
        "ts": order["created_at"],
        "cust": {"nm": customer["name"], "em": customer["email"]},
        "lines": order["items"],
        "cust_nt": order.get("customer_note", ""),
        **_noise(order_id),
    })


@router.post("/orders/action")
async def order_action(request: Request):
    caller = caller_for(request, "v1")
    body = await read_json(request)
    if body is None:
        raise V1Error(400)
    order = state.orders.get(str(body.get("id")))
    action = body.get("action")
    amt = body.get("amt")
    flag = body.get("flag")
    endpoint = "POST /v1/orders/action"
    if order is None or action not in ("rf", "cx", "pc"):
        raise V1Error(400)

    if action == "cx":
        if order["status"] != "pending":
            raise V1Error(400)
        if flag is True:
            order["status"] = "cancelled"
            state.add_audit(token_label=caller.label, agent=caller.agent, token=caller.token,
                            endpoint=endpoint, decision="allowed", reason="v1 cancel", order_id=order["order_id"])
        return respond(request, {"ok": True})

    if not isinstance(amt, (int, float)) or isinstance(amt, bool) or amt <= 0:
        raise V1Error(400)
    cents = round(amt * 100) if action == "rf" else round(amt)
    if order["status"] not in ("paid", "delivered", "refunded") or cents > order["total_cents"]:
        raise V1Error(400)
    if flag is not True:
        return respond(request, {"ok": True})  # silent no-op

    state.v1_stale.pop(order["order_id"], None)   # a new write refreshes the cache
    before = copy.deepcopy(order)
    state.add_ledger(order["order_id"], cents, "refund", "v1", caller.label, caller.agent)
    state.add_audit(token_label=caller.label, agent=caller.agent, token=caller.token, endpoint=endpoint,
                    decision="allowed", reason=f"v1 {action} {cents} cents", order_id=order["order_id"])
    if state.settings["lose_next_response"]:
        state.update_settings(lose_next_response=False)
        state.v1_stale[order["order_id"]] = (before, time.monotonic() + STALE_READ_SECONDS)
        state.bus.emit("chaos", what="lost_response", order_id=order["order_id"], api="v1")
        raise V1Error(504, "<html><body><h1>504 Gateway Time-out</h1></body></html>")
    return respond(request, {"ok": True, "ref": f"R{len(state.ledger):05d}"})
