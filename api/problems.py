"""problem+json (RFC 9457) helpers and the v2 error catalog."""

from fastapi.responses import JSONResponse

DOCS = "https://docs.shopco.dev/errors/"

# code -> (status, title, retryable)
CATALOG: dict[str, tuple[int, str, bool | None]] = {
    "refund-too-large": (422, "Refund amount too large", False),
    "order-not-refundable": (409, "Order is not refundable", False),
    "order-not-cancellable": (409, "Order cannot be cancelled", False),
    "insufficient-scope": (403, "Token lacks the required scope", False),
    "idempotency-key-required": (400, "Idempotency-Key header required", False),
    "idempotency-key-reused": (422, "Idempotency-Key reused with a different body", False),
    "rate-limited": (429, "Too many requests", True),
    "upstream-timeout": (504, "Upstream timeout", True),
    "approval-required": (202, "Human approval required", None),
    "order-not-found": (404, "Order not found", False),
    "customer-not-found": (404, "Customer not found", False),
    "validation-failed": (400, "Request body is invalid", False),
    "recipient-not-allowed": (403, "Recipient not allowed", False),
    "unauthorized": (401, "Missing or unknown bearer token", False),
}


class Problem(Exception):
    def __init__(self, code: str, detail: str, next_action: str | None = None,
                 headers: dict[str, str] | None = None, **extra):
        status, title, retryable = CATALOG[code]
        self.code = code
        self.status = status
        self.headers = headers or {}
        self.body = {
            "type": DOCS + code,
            "title": title,
            "status": status,
            "detail": detail,
            "retryable": retryable if retryable is not None else "n/a",
            "next_action": next_action,
            **extra,
        }
        if next_action is None:
            self.body.pop("next_action")
        super().__init__(detail)

    def response(self) -> JSONResponse:
        return JSONResponse(self.body, status_code=self.status, headers=self.headers,
                            media_type="application/problem+json")
