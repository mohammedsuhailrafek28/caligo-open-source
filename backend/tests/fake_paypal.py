"""In-memory fake of the PayPal sandbox HTTP API, served through httpx.MockTransport.

No socket is ever opened. It models just enough of OAuth + Invoicing v2 for the adapter: idempotent create by
PayPal-Request-Id, unique invoice numbers, send, get, payments/refunds, and injectable faults.
"""
import base64
import itertools
import json
import uuid
from collections import deque

import httpx

from app.paypal_sandbox import minor_to_value

CLIENT_ID = "fake-example-client-id-0001"
CLIENT_SECRET = "fake-example-client-secret-9f8e7d"  # test-only placeholder
MERCHANT = "merchant@example.com"
SETTINGS_CREDENTIALS = {"paypal_client_id": CLIENT_ID, "paypal_client_secret": CLIENT_SECRET}
BASE = "https://api-m.sandbox.paypal.com"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePayPal:
    def __init__(self, *, expires_in: int = 3600, merchant: str = MERCHANT) -> None:
        self.invoices: dict[str, dict] = {}
        self.by_request_id: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []  # (method, path)
        self.auth_headers: list[str] = []
        self.faults: dict[tuple[str, str], deque] = {}
        self.expires_in = expires_in
        self.merchant = merchant
        self.tokens_issued = 0
        self.valid_token: str | None = None
        self.on_request = None  # optional hook(method, path) called before handling
        self.create_style = "href"  # shape of the 201 body: href | legacy_host | link_description | links | representation
        self.create_response = None  # optional factory(invoice_id) -> httpx.Response, AFTER the invoice was really created
        self.create_headers: list[dict] = []  # headers of every create request
        self.create_bodies: list[dict] = []  # JSON body of every create request
        self.invoicer_mode = "default"  # default | omit | empty | other : what the stored invoice shows as `invoicer`
        self.other_invoicer = "someone-else@example.com"  # shown when invoicer_mode == "other"
        self.validate_invoicer = True  # reject a requested invoicer email that is not the token's business account
        self._seq = itertools.count(1)

    # --- test controls ------------------------------------------------------------------
    def fault(self, method: str, path_suffix: str, *actions) -> None:
        """Queue actions for matching calls: an int status, 'timeout', 'connect', or 'create_then_timeout'."""
        self.faults.setdefault((method, path_suffix), deque()).extend(actions)

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))

    def only(self) -> dict:
        assert len(self.invoices) == 1, len(self.invoices)
        return next(iter(self.invoices.values()))

    def pay(self, invoice: dict, amount_minor: int | None = None, kind: str = "PAYPAL", status: str | None = None) -> None:
        total = self._minor({"amount": invoice["amount"]})
        amount = total if amount_minor is None else amount_minor
        paid = sum(self._minor(t) for t in invoice["payments"]["transactions"]) + amount
        invoice["payments"]["transactions"].append({
            "payment_id": f"PAY-{uuid.uuid4().hex[:12].upper()}" if kind == "PAYPAL" else None, "type": kind,
            "amount": {"currency_code": invoice["amount"]["currency_code"], "value": minor_to_value(amount, invoice["amount"]["currency_code"])}})
        invoice["status"] = status or ("PAID" if paid == total else "PARTIALLY_PAID")

    def refund(self, invoice: dict, amount_minor: int | None = None) -> None:
        paid = sum(self._minor(t) for t in invoice["payments"]["transactions"])
        amount = paid if amount_minor is None else amount_minor
        invoice["refunds"]["transactions"].append({
            "refund_id": f"REF-{uuid.uuid4().hex[:12].upper()}", "type": "PAYPAL",
            "amount": {"currency_code": invoice["amount"]["currency_code"], "value": minor_to_value(amount, invoice["amount"]["currency_code"])}})
        refunded = sum(self._minor(t) for t in invoice["refunds"]["transactions"])
        invoice["status"] = "REFUNDED" if refunded == paid else "PARTIALLY_REFUNDED"

    @staticmethod
    def _minor(txn: dict) -> int:
        value = txn["amount"]["value"]
        return int(value.replace(".", "")) if "." in value else int(value)

    # --- the transport ------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        assert request.url.host == "api-m.sandbox.paypal.com", f"unexpected host {request.url.host}"
        assert request.url.scheme == "https"
        self.calls.append((method, path))
        if self.on_request:
            self.on_request(method, path)
        for (m, suffix), queue in self.faults.items():
            if m == method and path.endswith(suffix) and queue:
                action = queue.popleft()
                if action == "timeout":
                    raise httpx.ReadTimeout("simulated timeout", request=request)
                if action == "connect":
                    raise httpx.ConnectError("simulated connect failure", request=request)
                if action == "create_then_timeout":
                    self._create(request)
                    raise httpx.ReadTimeout("simulated timeout after processing", request=request)
                if isinstance(action, tuple):  # (status, headers)
                    return self._json(action[0], {"name": "SIMULATED"}, action[1])
                return self._json(int(action), {"name": "INTERNAL_SERVER_ERROR" if int(action) >= 500 else "SIMULATED"})
        if path == "/v1/oauth2/token":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {self.valid_token}" or self.valid_token is None:
            return self._json(401, {"name": "AUTHENTICATION_FAILURE"})
        self.auth_headers.append(request.headers["authorization"])
        if method == "POST" and path == "/v2/invoicing/invoices":
            return self._create(request)
        invoice_id = path.rsplit("/", 2)[-2] if path.endswith("/send") else path.rsplit("/", 1)[-1]
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            return self._json(404, {"name": "INVALID_RESOURCE_ID"})
        if method == "POST" and path.endswith("/send"):
            if invoice["status"] != "DRAFT":
                return self._json(422, {"name": "INVALID_INVOICE_STATUS"})
            invoice["status"] = "SENT"
            invoice["sent_count"] = invoice.get("sent_count", 0) + 1
            return self._json(202, {})
        if method == "GET":
            return self._json(200, invoice)
        return self._json(405, {})

    def _token(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("authorization", "")
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if header != f"Basic {expected}" or b"grant_type=client_credentials" not in request.content:
            return self._json(401, {"name": "invalid_client"})
        self.tokens_issued += 1
        self.valid_token = f"fake-token-{self.tokens_issued}-{uuid.uuid4().hex}"
        return self._json(200, {"access_token": self.valid_token, "token_type": "Bearer", "expires_in": self.expires_in})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.create_headers.append({k.lower(): v for k, v in request.headers.items()})
        self.create_bodies.append(body)
        request_id = request.headers.get("paypal-request-id", "")
        if request_id and request_id in self.by_request_id:
            return self._created(self.by_request_id[request_id], request)
        requested = (body.get("invoicer") or {}).get("email_address")
        if self.validate_invoicer and requested is not None and str(requested).strip().lower() != self.merchant.lower():
            return self._json(422, {"name": "INVALID_PARAMETER_VALUE"})  # simulated: the real error name is unconfirmed
        number = body["detail"]["invoice_number"]
        if any(i["detail"]["invoice_number"] == number for i in self.invoices.values()):
            return self._json(422, {"name": "DUPLICATE_INVOICE_NUMBER"})
        invoice_id = f"INV2-{next(self._seq):04d}-{uuid.uuid4().hex[:8].upper()}"
        item = body["items"][0]
        self.invoices[invoice_id] = {
            "id": invoice_id, "status": "DRAFT",
            "detail": {"invoice_number": number, "currency_code": body["detail"]["currency_code"]},
            **self._invoicer_field(),
            "primary_recipients": body["primary_recipients"],
            "amount": {"currency_code": item["unit_amount"]["currency_code"], "value": item["unit_amount"]["value"]},
            "payments": {"transactions": []}, "refunds": {"transactions": []}, "request_body": body,
        }
        if request_id:
            self.by_request_id[request_id] = invoice_id
        return self._created(invoice_id, request)

    def _invoicer_field(self) -> dict:
        if self.invoicer_mode == "omit":
            return {}  # what the real Sandbox returned for a draft created without an invoicer
        if self.invoicer_mode == "empty":
            return {"invoicer": {}}
        if self.invoicer_mode == "other":
            return {"invoicer": {"email_address": self.other_invoicer}}
        return {"invoicer": {"email_address": self.merchant}}

    def _created(self, invoice_id: str, request: httpx.Request) -> httpx.Response:
        if self.create_response is not None:
            return self.create_response(invoice_id)
        link = f"{BASE}/v2/invoicing/invoices/{invoice_id}"
        style = self.create_style
        if style == "legacy_host":
            return self._json(201, {"rel": "self", "href": link.replace("api-m.sandbox", "api.sandbox"), "method": "GET"})
        if style == "link_description":
            return self._json(201, {"rel": "self", "href": link, "method": "GET"})
        if style == "links":
            return self._json(201, {"links": [{"rel": "send", "method": "POST", "href": link + "/send"},
                                              {"rel": "self", "method": "GET", "href": link}]})
        if style == "representation":  # what `Prefer: return=representation` is documented to return
            return self._json(201, {**self.invoices[invoice_id], "links": [{"rel": "self", "method": "GET", "href": link}]})
        return self._json(201, {"href": link, "rel": "self"})
