"""Safety layer of the controlled PayPal Sandbox validation runner: the request allowlist, run state and workdir rules.

Nothing here talks to the network by itself. `RunnerTransport` wraps whatever transport it is given (the real one in a future approved run, a fake in
tests and in the offline rehearsal) and refuses, BEFORE sending, every request outside the stage that was armed:

  stage CREATE  OAuth token (<=2); POST /v2/invoicing/invoices once, with a body that is exactly one 1.00 USD item for the configured buyer;
                GET only of the invoice id that create response returned (<=3).
  stage SEND    OAuth token (<=2); POST /v2/invoicing/invoices/{that id}/send once, with the fixed body; GET of that id (<=2).
  stage READ    OAuth token (<=2); GET of the recorded invoice id (<=2).

Refunds, captures, transfers, cancels, mark-as-paid/record-payment, reminders, deletes, other invoices, other hosts (including every production
host), other schemes, ports, queries and fragments are never allowed. The pre-existing smoke draft is on a deny list. A mutation that was started
and did not end in a clean 2xx is recorded as AMBIGUOUS in the run state and blocks every later mutation: it is never retried.
"""
import json
import os
from contextlib import contextmanager
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

import httpx

from app.payment_provider import ProviderError
from app.paypal_sandbox import (
    INVOICES_PATH, SANDBOX_BASE_URL, TOKEN_PATH, created_invoice_id, is_definitive_rejection, minor_to_value, provider_debug_id, provider_error_name,
    value_to_minor,
)
from app.schemas import ProviderInvoiceState

SANDBOX_HOST = httpx.URL(SANDBOX_BASE_URL).host
RUN_AMOUNT_MINOR = 100
RUN_CURRENCY = "USD"
RUN_AMOUNT_VALUE = minor_to_value(RUN_AMOUNT_MINOR, RUN_CURRENCY)  # "1.00"
SEND_BODY = {"send_to_recipient": True, "send_to_invoicer": False}
# The invoice that already exists in the owner's Sandbox (see docs/STAGE_2D_SMOKE_TEST.md). The runner must never read, send or otherwise touch it.
DENIED_INVOICE_IDS = frozenset({"INV2-0000-AAAA-BBBB-CCCC"})
DENIED_INVOICE_NUMBER_PREFIX = "SMOKE-"
RUN_INVOICE_NUMBER_PREFIX = "PEM-"  # app.services.payments.invoice_number_for
TOKEN_BUDGET, CREATE_GET_BUDGET, SEND_GET_BUDGET, READ_GET_BUDGET = 2, 3, 2, 2
_BODY_KEYS = frozenset({"detail", "primary_recipients", "items", "configuration", "invoicer"})
_DETAIL_KEYS = frozenset({"invoice_number", "currency_code", "reference", "memo"})
_ITEM_KEYS = frozenset({"name", "quantity", "unit_amount"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{5,63}$")
_KEY = re.compile(r"^[A-Za-z_]{1,32}$")
_WORD = re.compile(r"^[A-Z_]{1,32}$")
_MAX_KEYS = 20
STATE_FILE = "run_state.json"
LOCK_FILE = ".stage.lock"


class Stage(str, Enum):
    CREATE = "create"
    SEND = "send"
    READ = "read"


_NO_PAYMENT_STATES = frozenset({"DRAFT", "SENT", "SCHEDULED", "UNPAID", "CANCELLED"})


def _transactions(container: object, id_key: str, failures: list[str], group: str) -> list[int]:
    """Minor-unit amounts of a payments/refunds group; every entry must be a PAYPAL-type transaction with an id and a 1.00-USD-denominated amount."""
    items = container.get("transactions") if isinstance(container, dict) else None
    if container is not None and not isinstance(container, dict) or (items is not None and not isinstance(items, list)):
        failures.append(f"{group}_shape")
        return []
    amounts = []
    for item in items or []:
        try:
            ok = isinstance(item, dict) and item.get("type") == "PAYPAL" and isinstance(item.get(id_key), str) and bool(item[id_key]) \
                and item["amount"]["currency_code"] == RUN_CURRENCY
            amounts.append(value_to_minor(item["amount"]["value"], RUN_CURRENCY) if ok else -1)
        except (KeyError, TypeError, ProviderError):
            amounts.append(-1)
        if amounts[-1] < 0:
            failures.append(f"{group}_transaction_data")
    return [a for a in amounts if a >= 0]


def authenticity_failures(data: object, *, invoice_id: str, merchant_email: str | None, buyer_email: str, invoice_number: str | None) -> list[str]:
    """Why a successful invoice read must NOT be promoted to AUTHENTICATED_PAYPAL_SANDBOX evidence (fixed reason words; empty list = promotable).

    The read must be exactly the run's own invoice: same id and invoice number, issued by the configured merchant, addressed to the configured buyer,
    1.00 USD, a recognised status, and payment/refund data that is complete and consistent with that status. Anything else stays UNATTESTED."""
    if not isinstance(data, dict):
        return ["not_an_object"]
    failures: list[str] = []
    if data.get("id") != invoice_id:
        failures.append("invoice_id_mismatch")
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    if not invoice_number:
        failures.append("no_expected_invoice_number")
    elif detail.get("invoice_number") != invoice_number:
        failures.append("invoice_number_mismatch")
    invoicer = data.get("invoicer") if isinstance(data.get("invoicer"), dict) else {}
    shown = invoicer.get("email_address")
    if not merchant_email:
        failures.append("no_expected_merchant")
    elif not isinstance(shown, str) or not shown.strip():
        failures.append("merchant_missing")
    elif shown.strip().lower() != merchant_email.strip().lower():
        failures.append("merchant_mismatch")
    recipients = data.get("primary_recipients")
    first = recipients[0] if isinstance(recipients, list) and len(recipients) == 1 and isinstance(recipients[0], dict) else {}
    to = (first.get("billing_info") or {}).get("email_address") if isinstance(first.get("billing_info"), dict) else None
    if not isinstance(to, str) or to.strip().lower() != buyer_email.strip().lower():
        failures.append("recipient_mismatch")
    amount = data.get("amount") if isinstance(data.get("amount"), dict) else {}
    if amount.get("currency_code") != RUN_CURRENCY or amount.get("value") != RUN_AMOUNT_VALUE:
        failures.append("amount_mismatch")
    status = data.get("status")
    if status not in {s.value for s in ProviderInvoiceState}:
        failures.append("status_unrecognised")
        return failures
    paid = _transactions(data.get("payments"), "payment_id", failures, "payments")
    _transactions(data.get("refunds"), "refund_id", failures, "refunds")
    total = sum(paid)
    if status in _NO_PAYMENT_STATES and (paid or (data.get("payments") or {}).get("transactions")):
        failures.append("payment_status_unverifiable")
    elif status == "PAID" and (not paid or total != RUN_AMOUNT_MINOR):
        failures.append("payment_status_unverifiable")
    elif status == "PARTIALLY_PAID" and not 0 < total < RUN_AMOUNT_MINOR:
        failures.append("payment_status_unverifiable")
    elif status not in _NO_PAYMENT_STATES and status not in ("PAID", "PARTIALLY_PAID", "PAYMENT_PENDING") and not paid:
        failures.append("payment_status_unverifiable")
    return failures


class StateCorrupt(Exception):
    """run_state.json cannot be trusted: the stage is refused (never repaired or guessed)."""


class StageBusy(Exception):
    """Another process holds this work directory's stage lock."""


class BlockedRequest(Exception):
    """An outbound request outside the armed stage's allowlist was refused before it was sent."""


@dataclass
class RunState:
    """Non-secret, per-run facts. Written to `run_state.json` in the run's work directory (never in the repository)."""

    run_id: str
    buyer_masked: str
    invoice_number: str
    create_attempted: bool = False
    create_outcome: str | None = None      # OK | AMBIGUOUS | REFUSED
    invoice_id: str | None = None
    payment_id: str | None = None          # Caligo's PaymentRecord id
    project_id: str | None = None
    draft_verified: bool = False
    send_attempted: bool = False
    send_outcome: str | None = None        # OK | AMBIGUOUS | REFUSED
    last_status: str | None = None         # provider invoice state word from the latest read
    last_mutation_http: int | None = None  # HTTP status of the latest create/send response (None when no response arrived)
    last_mutation_error: str | None = None  # PayPal's sanitized error name of that response
    last_mutation_debug_id: str | None = None  # PayPal's sanitized debug id of that response
    authenticated_payment_ids: list[str] = field(default_factory=list)
    ambiguous: bool = False
    stages_done: list[str] = field(default_factory=list)
    updated_at: str = ""

    def save(self, workdir: Path) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(dir=workdir, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(asdict(self), handle, indent=2)
        os.replace(tmp, workdir / STATE_FILE)

    @classmethod
    def load(cls, workdir: Path) -> "RunState | None":
        """The saved state, None if there is none, `StateCorrupt` if it cannot be trusted (unreadable, unknown or mistyped fields)."""
        path = workdir / STATE_FILE
        if not path.exists():
            return None
        try:
            state = cls(**json.loads(path.read_text(encoding="utf8")))
            if not all(isinstance(getattr(state, f), t) for f, t in _STATE_TYPES.items() if getattr(state, f) is not None):
                raise TypeError("field type")
            if any(value is not None and not pattern.fullmatch(value) for value, pattern in
                   ((state.last_mutation_error, _ERROR_NAME_WORD), (state.last_mutation_debug_id, _DEBUG_ID_WORD))):
                raise TypeError("unsafe error metadata")
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise StateCorrupt("the run state file is unreadable or has unexpected fields") from exc


_ERROR_NAME_WORD = re.compile(r"^[A-Z0-9_]{3,64}$")   # same shapes the sanitizers in paypal_sandbox accept
_DEBUG_ID_WORD = re.compile(r"^[A-Za-z0-9_.-]{4,64}$")
_STATE_TYPES = {"run_id": str, "buyer_masked": str, "invoice_number": str, "create_attempted": bool, "create_outcome": str, "invoice_id": str,
                "payment_id": str, "project_id": str, "draft_verified": bool, "send_attempted": bool, "send_outcome": str, "last_status": str,
                "last_mutation_http": int, "last_mutation_error": str, "last_mutation_debug_id": str,
                "authenticated_payment_ids": list, "ambiguous": bool, "stages_done": list}


@contextmanager
def stage_lock(workdir: Path):
    """Exclusive, non-blocking lock for the whole stage (check, persist 'attempted', mutate). Two concurrent stages in one directory cannot both proceed."""
    path = workdir / LOCK_FILE
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise StageBusy("another runner process holds this work directory (remove .stage.lock only if no runner is running)") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def refuse_unsafe_workdir(workdir: Path, repo_root: Path) -> str | None:
    """A reason to refuse this work directory, or None. It must not be inside (or contain) the repository, the smoke state or the home directory."""
    target = workdir.resolve()
    for protected in (repo_root, repo_root / "backend" / ".smoke"):
        resolved = protected.resolve()
        if target == resolved or resolved in target.parents or target in resolved.parents:
            return f"{target} is inside or contains a protected directory ({resolved})"
    home = Path.home().resolve()
    if target == home or target in home.parents:
        return f"{target} is the home directory or one of its parents"
    if target.parent == target:
        return "refusing a filesystem root"
    if target.exists() and not target.is_dir():
        return f"{target} is not a directory"
    return None


def _safe_keys(data: object) -> tuple[str, ...]:
    return tuple(sorted(k for k in data if isinstance(k, str) and _KEY.match(k)))[:_MAX_KEYS] if isinstance(data, dict) else ()


def _outcome_is_uncertain(response: httpx.Response) -> bool:
    """True when this answer to a state-changing request does not prove whether PayPal processed it.

    Unknown: 5xx, redirects, an unexpected 2xx, and every 4xx that is not a clean PayPal rejection (406, 415, 408, 409, a gateway body...).
    Known: 200/201/202 (processed), 429 (rate-limited before processing), a structured 400/404/422 or 401/403 (rejected)."""
    status = response.status_code
    if status >= 500 or 300 <= status < 400:
        return True
    if 200 <= status < 300:
        return status not in (200, 201, 202)
    return status != 429 and not is_definitive_rejection(response)


class RunnerTransport(httpx.BaseTransport):
    """The only path to PayPal in the runner. See the module docstring for exactly what it lets through."""

    def __init__(self, inner: httpx.BaseTransport, stage: Stage, *, buyer_email: str, invoice_id: str | None = None,
                 live: bool = False, before_mutation: Callable[[str], None] | None = None, merchant_email: str | None = None,
                 invoice_number: str | None = None) -> None:
        if invoice_id is not None and (not _ID.fullmatch(invoice_id) or invoice_id in DENIED_INVOICE_IDS):
            raise ValueError("invalid or forbidden invoice id")
        if stage in (Stage.SEND, Stage.READ) and invoice_id is None:
            raise ValueError("this stage needs the recorded invoice id")
        if stage is Stage.CREATE and invoice_id is not None:
            raise ValueError("the create stage must not name an invoice")
        self._inner, self.stage, self._buyer = inner, stage, buyer_email.strip().lower()
        self.live = live                       # True only when `inner` is the real network transport of an approved run
        self._merchant, self._invoice_number = merchant_email, invoice_number
        self.reads_seen = 0
        self.read_failures: tuple[str, ...] = ()  # why the latest read could not be promoted (fixed words)
        self._invoice_id = invoice_id          # the only invoice that may be read or sent (for CREATE: learned from the create response)
        self._before_mutation = before_mutation
        self._tokens = TOKEN_BUDGET
        self._reads = {Stage.CREATE: CREATE_GET_BUDGET, Stage.SEND: SEND_GET_BUDGET, Stage.READ: READ_GET_BUDGET}[stage]
        self._mutation_used = False
        self.mutation_sent = False
        self.mutation_status: int | None = None
        self.mutation_error_name: str | None = None   # PayPal's sanitized error `name` of a non-2xx mutation response
        self.mutation_debug_id: str | None = None     # PayPal's sanitized debug id of that response (for a support ticket)
        self.ambiguous_mutation = False
        self.created_invoice_id: str | None = None
        self.authenticated_read = False        # a GET of the named invoice returned 200 through `inner`
        self.statuses: list[int] = []          # HTTP status codes only
        self.response_keys: tuple[str, ...] = ()
        self.response_status_word: str | None = None
        self.transaction_types: tuple[str, ...] = ()

    @property
    def invoice_id(self) -> str | None:
        return self._invoice_id

    def _block(self, reason: str) -> BlockedRequest:
        return BlockedRequest(reason)

    def _classify(self, request: httpx.Request) -> str:
        url = request.url
        if url.scheme != "https" or url.host != SANDBOX_HOST or url.port is not None or url.userinfo or url.fragment or url.query:
            raise self._block("request outside the PayPal Sandbox allowlist")
        path, method = url.raw_path.decode(), request.method
        if path.startswith(INVOICES_PATH):
            if any(denied in path for denied in DENIED_INVOICE_IDS):
                raise self._block("the pre-existing Sandbox invoice must never be accessed")
        if method == "POST" and path == TOKEN_PATH:
            return "token"
        if method == "POST" and path == INVOICES_PATH and self.stage is Stage.CREATE:
            return "create"
        if self._invoice_id is not None:
            if method == "GET" and path == f"{INVOICES_PATH}/{self._invoice_id}":
                return "read"
            if method == "POST" and path == f"{INVOICES_PATH}/{self._invoice_id}/send" and self.stage is Stage.SEND:
                return "send"
        raise self._block("request not allowed in this stage")

    def _check_create_body(self, request: httpx.Request) -> None:
        try:
            body = json.loads(request.content)
            item = body["items"][0]
            recipient = body["primary_recipients"][0]
            ok = (set(body) <= _BODY_KEYS and set(body["detail"]) <= _DETAIL_KEYS and body.get("configuration", {"allow_tip": False}) == {"allow_tip": False}
                  and set(body.get("invoicer") or {}) <= {"email_address"} and set(item) <= _ITEM_KEYS and set(recipient) == {"billing_info"}
                  and set(recipient["billing_info"]) == {"email_address"}
                  and len(body["items"]) == 1 and item["quantity"] == "1" and item["unit_amount"] == {"currency_code": RUN_CURRENCY, "value": RUN_AMOUNT_VALUE}
                  and body["detail"]["currency_code"] == RUN_CURRENCY and str(body["detail"]["invoice_number"]).startswith(RUN_INVOICE_NUMBER_PREFIX)
                  and not str(body["detail"]["invoice_number"]).startswith(DENIED_INVOICE_NUMBER_PREFIX)
                  and len(body["primary_recipients"]) == 1
                  and str(body["primary_recipients"][0]["billing_info"]["email_address"]).strip().lower() == self._buyer)
        except (ValueError, KeyError, IndexError, TypeError):
            ok = False
        if not ok:
            raise self._block("create body is not exactly one 1.00 USD item for the configured buyer")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        kind = self._classify(request)
        if kind == "token":
            if self._tokens <= 0:
                raise self._block("OAuth request budget exhausted")
            self._tokens -= 1
            return self._record(self._inner.handle_request(request), kind, request)
        if kind == "read":
            if self._reads <= 0:
                raise self._block("invoice read budget exhausted")
            self._reads -= 1
            return self._record(self._inner.handle_request(request), kind, request)
        return self._mutate(request, kind)

    def _mutate(self, request: httpx.Request, kind: str) -> httpx.Response:
        if self._mutation_used:
            raise self._block("one mutation per stage: a second create/send is refused (no automatic retry)")
        if kind == "create":
            self._check_create_body(request)
        elif json.loads(request.content or b"null") != SEND_BODY:
            raise self._block("send body is not the fixed send body")
        self._mutation_used = True
        if self._before_mutation is not None:
            self._before_mutation(kind)  # the caller persists "attempted" BEFORE anything is sent
        self.mutation_sent = True
        try:
            response = self._inner.handle_request(request)
        except Exception:
            self.ambiguous_mutation = True  # sent, no answer: unknown whether PayPal processed it
            raise
        self.mutation_status = response.status_code
        if response.status_code >= 300:
            self.mutation_error_name, self.mutation_debug_id = provider_error_name(response), provider_debug_id(response)
        if _outcome_is_uncertain(response):
            self.ambiguous_mutation = True
        if kind == "create" and response.status_code in (200, 201):
            self._learn_created_id(response)
        return self._record(response, kind, request)

    def _learn_created_id(self, response: httpx.Response) -> None:
        try:
            response.read()
            new_id = created_invoice_id(json.loads(response.content))
        except Exception:  # unreadable create response: ambiguous, and no GET will be allowed
            self.ambiguous_mutation = True
            return
        if new_id in DENIED_INVOICE_IDS or not _ID.fullmatch(new_id):  # an id that could widen the GET allowlist (slashes, dots) is never adopted
            self.ambiguous_mutation = True
            return
        self._invoice_id = self.created_invoice_id = new_id

    def _record(self, response: httpx.Response, kind: str, request: httpx.Request) -> httpx.Response:
        self.statuses.append(response.status_code)
        if kind == "read":
            self._remember_shape(response)
            self.reads_seen += 1
            self.authenticated_read = self._promotable(response, request)
        return response

    def _promotable(self, response: httpx.Response, request: httpx.Request) -> bool:
        """True only for a 200 JSON read of the run's own invoice, through the real transport, that passes every authenticity check."""
        failures: list[str] = []
        if not self.live:
            failures.append("transport_not_live")
        if response.status_code != 200:
            failures.append("status_not_200")
        if request.url.scheme != "https" or request.url.host != SANDBOX_HOST or request.url.raw_path.decode() != f"{INVOICES_PATH}/{self._invoice_id}":
            failures.append("origin_or_path_mismatch")
        try:
            response.read()
            data = json.loads(response.content)
        except Exception:  # noqa: BLE001 - unreadable body: nothing to verify
            data = None
        failures += authenticity_failures(data, invoice_id=self._invoice_id or "", merchant_email=self._merchant, buyer_email=self._buyer,
                                          invoice_number=self._invoice_number)
        self.read_failures = tuple(dict.fromkeys(failures))
        return not failures

    def _remember_shape(self, response: httpx.Response) -> None:
        """Key NAMES, the status word and the transaction `type` words of the invoice response, for diagnosing an unexpected shape. Never values."""
        try:
            response.read()
            data = json.loads(response.content)
        except Exception:
            return
        self.response_keys = _safe_keys(data)
        status = data.get("status") if isinstance(data, dict) else None
        self.response_status_word = status if isinstance(status, str) and _WORD.fullmatch(status) else None
        types = []
        for group in ("payments", "refunds"):
            items = (data.get(group) or {}).get("transactions") if isinstance(data, dict) and isinstance(data.get(group), dict) else None
            for item in items if isinstance(items, list) else []:
                word = item.get("type") if isinstance(item, dict) else None
                types.append(f"{group}:{word if isinstance(word, str) and _WORD.fullmatch(word) else '?'}")
        self.transaction_types = tuple(types[:_MAX_KEYS])

    def close(self) -> None:
        self._inner.close()
