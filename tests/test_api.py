"""API behavior the scenes depend on: idempotency, scopes, errors, rate limit, v1 traps."""

import pytest
from fastapi.testclient import TestClient

from api import injection
from api.main import app
from api.state import TOKEN_ADMIN, TOKEN_SUPPORT_AGENT, state

AGENT = {"Authorization": f"Bearer {TOKEN_SUPPORT_AGENT}", "X-Agent-Label": "pytest"}
ADMIN = {"Authorization": f"Bearer {TOKEN_ADMIN}"}


@pytest.fixture(autouse=True)
def client():
    state.reset(emit=False)
    with TestClient(app) as c:
        yield c


def refund(c, order_id, amount, key="key-00000001", reason="damaged", dry_run=False, headers=AGENT):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return c.post(f"/v2/orders/{order_id}/refunds", params={"dry_run": str(dry_run).lower()},
                  json={"amount_cents": amount, "reason": reason}, headers=h)


# ---------------------------------------------------------------- v2
def test_get_order_is_small_and_groups_user_content(client):
    r = client.get("/v2/orders/ord_8841", headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert body["refundable_cents"] == 4990
    assert "customer_note" in body["user_content"]
    assert "items" not in body
    assert "items" in client.get("/v2/orders/ord_8841?fields=items", headers=AGENT).json()


def test_scene3_dry_run_then_refund(client):
    dry = refund(client, "ord_8841", 4990, key=None, dry_run=True)
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert state.ledger == []
    r = refund(client, "ord_8841", 4990)
    assert r.status_code == 201
    assert r.json()["order"]["refundable_cents"] == 0
    assert len(state.ledger) == 1


def test_scene4_refund_too_large_names_the_max(client):
    r = refund(client, "ord_8841", 5990)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["max_refundable_cents"] == 4990
    assert body["retryable"] is False
    assert "4990" in body["next_action"]
    # Same key, corrected amount: allowed because the failed call stored nothing.
    assert refund(client, "ord_8841", 4990).status_code == 201


def test_scene5_lost_response_retry_refunds_once(client):
    state.update_settings(lose_next_response=True)
    first = refund(client, "ord_8842", 12900, key="retry-key-1")
    assert first.status_code == 504 and first.json()["retryable"] is True
    second = refund(client, "ord_8842", 12900, key="retry-key-1")
    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(state.ledger) == 1


def test_idempotency_key_required_and_reuse_detected(client):
    assert refund(client, "ord_8841", 1000, key=None).json()["type"].endswith("idempotency-key-required")
    assert refund(client, "ord_8841", 1000, key="same-key-1").status_code == 201
    r = refund(client, "ord_8841", 2000, key="same-key-1")
    assert r.status_code == 422 and r.json()["type"].endswith("idempotency-key-reused")


def test_pending_order_points_to_cancel(client):
    r = refund(client, "ord_9001", 7000)
    assert r.status_code == 409
    assert "cancel_order" in r.json()["next_action"]
    assert client.post("/v2/orders/ord_9001/cancel", headers=AGENT).json()["status"] == "cancelled"


def test_validation_lists_allowed_reasons(client):
    r = refund(client, "ord_8841", 49.9, reason="broken")
    assert r.status_code == 400
    fields = {e["field"] for e in r.json()["errors"]}
    assert fields == {"amount_cents", "reason"}


def test_email_needs_scope_and_is_audited(client):
    r = client.post("/v2/emails", headers=AGENT, json={"order_id": "ord_8841", "subject": "x", "body": "y"})
    assert r.status_code == 403
    assert r.json()["missing_scope"] == "emails:send"
    assert state.audit[-1]["decision"] == "denied"
    # Even an admin cannot email an arbitrary address.
    r = client.post("/v2/emails", headers=ADMIN, json={"order_id": "ord_8841", "to": injection.ATTACKER_EMAIL,
                                                       "subject": "x", "body": "y"})
    assert r.status_code == 403 and r.json()["type"].endswith("recipient-not-allowed")


def test_mass_refund_hits_approval_and_audit_records_payload(client):
    client.post("/admin/scenes/injection")
    client.get("/v2/orders/ord_8841", headers=AGENT)          # agent reads the poisoned note
    assert refund(client, "ord_8841", 4990, key="mass-1").status_code == 201
    r = refund(client, "ord_8842", 12900, key="mass-2")
    assert r.status_code == 202 and r.json()["approval_id"].startswith("apr_")
    r = refund(client, "ord_8843", 2500, key="mass-3")
    assert r.status_code == 202
    held = [a for a in state.audit if a["decision"] == "held"]
    assert len(held) == 2 and "ord_8841" in held[0]["context"]
    assert len(state.ledger) == 1


def test_rate_limit_v2_has_headers(client):
    state.update_settings(rate_limit=2)
    for _ in range(2):
        assert client.get("/v2/orders/ord_8841", headers=AGENT).status_code == 200
    r = client.get("/v2/orders/ord_8841", headers=AGENT)
    assert r.status_code == 429
    assert "Retry-After" in r.headers and r.headers["RateLimit-Limit"] == "2"
    assert r.json()["retryable"] is True


def test_unknown_token(client):
    assert client.get("/v2/orders/ord_8841").status_code == 401
    assert client.get("/v1/orders/ord_8841").json() == {"error": "Invalid request"}


# ---------------------------------------------------------------- v1
def act(c, **body):
    return c.post("/v1/orders/action", headers=AGENT, json=body)


def test_v1_rejects_readable_action_names(client):
    r = act(client, id="ord_8841", action="refund", amt=49.9, flag=True)
    assert r.status_code == 400 and r.json() == {"error": "Invalid request"}


def test_v1_cents_vs_dollars(client):
    assert act(client, id="ord_8841", action="rf", amt=4990, flag=True).status_code == 400   # $4990 > total
    assert act(client, id="ord_8841", action="rf", amt=49.9, flag=True).status_code == 200
    assert state.ledger[-1]["amount_cents"] == 4990


def test_v1_flag_false_is_a_silent_noop(client):
    assert act(client, id="ord_8841", action="rf", amt=49.9, flag=False).json() == {"ok": True}
    assert state.ledger == []


def test_v1_lost_response_retry_refunds_twice(client):
    state.update_settings(lose_next_response=True)
    assert act(client, id="ord_8842", action="rf", amt=129, flag=True).status_code == 504
    assert act(client, id="ord_8842", action="rf", amt=129, flag=True).status_code == 200
    assert len(state.ledger) == 2 and state.ledger[1]["duplicate"]
    assert state.orders["ord_8842"]["refunded_cents"] == 25800


def test_v1_get_is_noisy(client):
    r = client.get("/v1/orders/ord_8841", headers=AGENT)
    assert len(r.content) > 3000


# ---------------------------------------------------------------- injection + admin
def test_hidden_injection_is_invisible_but_present():
    note = injection.note_for("hidden")
    visible = "".join(ch for ch in note if not 0xE0000 <= ord(ch) <= 0xE007F and ch != "​")
    assert "refund" not in visible.split("<!--")[0].lower()
    decoded = "".join(chr(ord(ch) - 0xE0000) for ch in note if 0xE0020 <= ord(ch) <= 0xE007E)
    assert injection.ATTACKER_EMAIL in decoded


def test_scene_presets_load(client):
    for scene in client.get("/admin/scenes").json():
        r = client.post(f"/admin/scenes/{scene['id']}")
        assert r.status_code == 200
        for k, v in scene["preset"].items():
            assert r.json()["settings"][k] == v


def test_events_are_emitted(client):
    before = len(state.bus.history)
    refund(client, "ord_8841", 4990)
    types = [e["type"] for e in list(state.bus.history)[before:]]
    assert "ledger" in types and "audit" in types and "http" in types
