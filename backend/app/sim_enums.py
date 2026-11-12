"""Enumerations for the SIMULATED merchant state (S1). Kept apart from app.schemas: nothing here is part of the public API contract,
and nothing here is a real merchant or payment-provider concept."""
from enum import Enum


class OrderState(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    FULFILLED = "FULFILLED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


# Forward-only transition table. REFUNDED and CANCELLED are terminal: nothing leaves them. FAILED -> PAID exists because a merchant may
# have failed an order locally (timeout, crash) although the provider evidence later shows the payment succeeded.
ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING: frozenset({OrderState.PAID, OrderState.FAILED, OrderState.CANCELLED}),
    OrderState.FAILED: frozenset({OrderState.PAID, OrderState.CANCELLED}),
    OrderState.PAID: frozenset({OrderState.FULFILLED, OrderState.REFUNDED, OrderState.CANCELLED}),  # CANCELLED: guarded recovery of a duplicate only
    OrderState.FULFILLED: frozenset({OrderState.REFUNDED}),
    OrderState.REFUNDED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


class FulfillmentState(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"  # irreversible: a parcel that left cannot be un-sent by a database update
    CANCELLED = "CANCELLED"


class MovementKind(str, Enum):
    RESERVE = "RESERVE"
    DEDUCT = "DEDUCT"
    RELEASE = "RELEASE"  # stock returned; only ever recorded with a reason and only for goods that did not ship


class EventSource(str, Enum):
    """Every incoming event in this simulation is explicitly synthetic; there is no real provider source."""

    SIMULATED_PROVIDER_WEBHOOK = "SIMULATED_PROVIDER_WEBHOOK"
    SIMULATED_MERCHANT_JOB = "SIMULATED_MERCHANT_JOB"


class EventType(str, Enum):
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED"
    PAYMENT_REFUNDED = "PAYMENT_REFUNDED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class ReplayStatus(str, Enum):
    FIRST = "FIRST"  # the first delivery of this (source, event key) in the project
    REPLAY_IDENTICAL = "REPLAY_IDENTICAL"  # same key, same payload digest: a redelivery
    REPLAY_DIFFERENT_PAYLOAD = "REPLAY_DIFFERENT_PAYLOAD"  # same key, different content: never applied


class Processor(str, Enum):
    SAFE = "SAFE"
    FAULTY_FIXTURE = "FAULTY_FIXTURE"  # deliberately wrong behavior, only ever created by app.simulation.faulty (test/demo fixtures)


class ProcessingOutcome(str, Enum):
    APPLIED = "APPLIED"
    DUPLICATE_NO_EFFECT = "DUPLICATE_NO_EFFECT"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_AMOUNT_MISMATCH = "REJECTED_AMOUNT_MISMATCH"
    REJECTED_CURRENCY_MISMATCH = "REJECTED_CURRENCY_MISMATCH"
    REJECTED_UNKNOWN_PAYMENT = "REJECTED_UNKNOWN_PAYMENT"
    REJECTED_INCOMPLETE_EVIDENCE = "REJECTED_INCOMPLETE_EVIDENCE"
    REJECTED_CONFLICTING_REPLAY = "REJECTED_CONFLICTING_REPLAY"
    REJECTED_CONTRADICTED_BY_PROVIDER = "REJECTED_CONTRADICTED_BY_PROVIDER"
    REJECTED_NO_MERCHANT_ORDER = "REJECTED_NO_MERCHANT_ORDER"
    HELD_AWAITING_PROVIDER_EVIDENCE = "HELD_AWAITING_PROVIDER_EVIDENCE"
    FAULTY_APPLIED = "FAULTY_APPLIED"  # what the deliberately faulty fixture did; never produced by the safe processor


class AuditAction(str, Enum):
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED"
    INVENTORY_MOVEMENT = "INVENTORY_MOVEMENT"
    FULFILLMENT_CREATED = "FULFILLMENT_CREATED"
    FULFILLMENT_DISPATCHED = "FULFILLMENT_DISPATCHED"
    FULFILLMENT_CANCELLED = "FULFILLMENT_CANCELLED"
    DISPATCH_PROTECTED = "DISPATCH_PROTECTED"  # a cancellation was refused because the parcel had already left
    EVENT_PROCESSED = "EVENT_PROCESSED"
    OPERATION_DUPLICATE_SUPPRESSED = "OPERATION_DUPLICATE_SUPPRESSED"


class DivergenceCode(str, Enum):
    PROVIDER_PAID_MERCHANT_NOT_PAID = "PROVIDER_PAID_MERCHANT_NOT_PAID"
    PROVIDER_REFUNDED_MERCHANT_STILL_PAID = "PROVIDER_REFUNDED_MERCHANT_STILL_PAID"
    MERCHANT_PAID_PROVIDER_NOT_PAID = "MERCHANT_PAID_PROVIDER_NOT_PAID"
    MULTIPLE_ORDERS_FOR_ONE_PAYMENT = "MULTIPLE_ORDERS_FOR_ONE_PAYMENT"
    DUPLICATE_INVENTORY_DEDUCTION = "DUPLICATE_INVENTORY_DEDUCTION"
    DUPLICATE_FULFILLMENT = "DUPLICATE_FULFILLMENT"
    ORDER_AMOUNT_OR_CURRENCY_MISMATCH = "ORDER_AMOUNT_OR_CURRENCY_MISMATCH"


# --- guarded recovery (S2) -----------------------------------------------------------------------------------------------------

class RecoveryKind(str, Enum):
    DUPLICATE_ORDERS = "DUPLICATE_ORDERS"  # scenario A
    PAID_NOT_APPLIED = "PAID_NOT_APPLIED"  # scenario B
    STALE_PAID_AFTER_REFUND = "STALE_PAID_AFTER_REFUND"  # scenario C
    FALSE_HEALTHY = "FALSE_HEALTHY"  # scenario D (diagnostic only)


class PlanStatus(str, Enum):
    """How a plan was born. Everything after that (APPROVED, EXECUTED) is a recorded event; a plan row is never rewritten."""

    PROPOSED = "PROPOSED"  # actionable: may be approved and executed
    NEEDS_REVIEW = "NEEDS_REVIEW"  # evidence is ambiguous (for example no certain canonical order): manual review, never executable
    BLOCKED = "BLOCKED"  # a safety precondition failed (provider evidence missing/incomplete/contradicting): never executable
    ADVISORY = "ADVISORY"  # diagnostic recommendation only; there is nothing to execute


class PlanState(str, Enum):
    """Derived current state of a plan: its birth status, then APPROVED, then EXECUTED."""

    PROPOSED = "PROPOSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"
    ADVISORY = "ADVISORY"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"


class StepKind(str, Enum):
    CANCEL_DUPLICATE_FULFILLMENT = "CANCEL_DUPLICATE_FULFILLMENT"
    RELEASE_STOCK = "RELEASE_STOCK"
    CANCEL_DUPLICATE_ORDER = "CANCEL_DUPLICATE_ORDER"
    REPLAY_PAID_EVENT = "REPLAY_PAID_EVENT"  # the existing idempotent event processor
    ORDER_TO_REFUNDED = "ORDER_TO_REFUNDED"
    CANCEL_PENDING_FULFILLMENT = "CANCEL_PENDING_FULFILLMENT"


class RecoveryEventType(str, Enum):
    PLAN_CREATED = "PLAN_CREATED"
    APPROVED = "APPROVED"
    APPROVAL_REFUSED = "APPROVAL_REFUSED"
    STEP_APPLIED = "STEP_APPLIED"
    STEP_ALREADY_DONE = "STEP_ALREADY_DONE"
    EXECUTED = "EXECUTED"
    EXECUTION_REFUSED = "EXECUTION_REFUSED"
