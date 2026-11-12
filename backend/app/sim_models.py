"""SIMULATED merchant business state (S1): orders, inventory, fulfillment, incoming events and an audit trail.

Everything here is a local simulation of a merchant system next to Caligo's real payment evidence. It never calls a provider, and merchant
state is never provider-authoritative: the provider side is only ever read from the existing PaymentRecord / PaymentObservation evidence.

Integrity rules enforced by the database (SQLite triggers + CHECKs), not only by the services:
* events, inventory items, inventory movements and audit records are append-only;
* an order's / fulfillment's identity (project, payment, business key, amount, ...) never changes, and a DISPATCHED fulfillment can never
  leave DISPATCHED or lose its dispatch time;
* every child row belongs to the same project as the parent it references (no cross-project references);
* one business operation (an operation key) can exist at most once per project.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DDL, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, event, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, UTCDateTime, enum_check, make_append_only, monotonic_utcnow, new_id, utcnow
from app.sim_enums import (
    AuditAction, EventSource, EventType, FulfillmentState, MovementKind, OrderState, PlanStatus, ProcessingOutcome, Processor,
    RecoveryEventType, RecoveryKind, ReplayStatus,
)

_CURRENCY = "length(currency) = 3 AND currency = upper(currency)"


def same_project_trigger(table: str, column: str, parent: str) -> tuple[str, str]:
    name = f"{table}_{column}_same_project"
    return name, (f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN NEW.{column} IS NOT NULL AND "
                  f"(SELECT project_id FROM {parent} WHERE id = NEW.{column}) IS NOT NEW.project_id "
                  f"BEGIN SELECT RAISE(ABORT, '{table}.{column} must reference the same project'); END;")


def identity_trigger(table: str, columns: tuple[str, ...]) -> tuple[str, str]:
    name = f"{table}_identity_immutable"
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)
    return name, (f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {changed} "
                  f"BEGIN SELECT RAISE(ABORT, '{table} identity is immutable'); END;")


def dispatch_protection_trigger() -> tuple[str, str]:
    name = "sim_fulfillments_dispatch_is_final"
    return name, ("CREATE TRIGGER sim_fulfillments_dispatch_is_final BEFORE UPDATE ON sim_fulfillments WHEN OLD.state = 'DISPATCHED' AND "
                  "(NEW.state IS NOT 'DISPATCHED' OR NEW.dispatched_at IS NOT OLD.dispatched_at) "
                  "BEGIN SELECT RAISE(ABORT, 'a dispatched fulfillment cannot be changed'); END;")


def install_triggers(model: type, triggers: list[tuple[str, str]]) -> None:
    for _name, sql in triggers:
        event.listen(model.__table__, "after_create", DDL(sql).execute_if(dialect="sqlite"))


class SimInventoryItem(Base):
    """A stock-keeping unit and its INITIAL stock. The current balance is derived from the movements, never stored."""

    __tablename__ = "sim_inventory_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    initial_stock: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "sku", name="uq_sim_inventory_items_project_sku"),
        CheckConstraint("initial_stock >= 0", name="ck_sim_inventory_items_stock"),
    )


make_append_only(SimInventoryItem)


class SimOrder(Base):
    """A merchant order tied to one provider payment of the SAME project. State is the merchant's own view (never provider truth);
    every state change is mirrored in sim_audit_records. `business_key` is the unique business operation identity."""

    __tablename__ = "sim_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payment_records.id"), index=True)
    business_key: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(ForeignKey("sim_inventory_items.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)  # integer minor units
    currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(16), default=OrderState.PENDING.value)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # optimistic concurrency
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("project_id", "business_key", name="uq_sim_orders_project_business_key"),
        enum_check("state", OrderState, "ck_sim_orders_state"),
        CheckConstraint("quantity >= 1 AND amount_minor >= 1", name="ck_sim_orders_positive"),
        CheckConstraint(_CURRENCY, name="ck_sim_orders_currency"),
    )


class SimFulfillment(Base):
    """Shipment state of one order. DISPATCHED is final and carries the dispatch time: that is the evidence the parcel left."""

    __tablename__ = "sim_fulfillments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("sim_orders.id"), index=True)
    operation_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default=FulfillmentState.PENDING.value)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("project_id", "operation_key", name="uq_sim_fulfillments_project_operation"),
        enum_check("state", FulfillmentState, "ck_sim_fulfillments_state"),
        CheckConstraint("(state = 'DISPATCHED') = (dispatched_at IS NOT NULL)", name="ck_sim_fulfillments_dispatch_evidence"),
    )


class SimIncomingEvent(Base):
    """One DELIVERY of an incoming (simulated) event. Redeliveries are separate rows, never merged or discarded. What happened when
    the event was processed lives in sim_audit_records, because a receipt must never be rewritten."""

    __tablename__ = "sim_incoming_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payment_records.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    event_key: Mapped[str] = mapped_column(String(100))  # the sender's event id: a CLAIM, not an identity we trust
    provider_txn_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(String(24))
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = the sender did not state it (incomplete evidence)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    asserted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # sender-claimed time; never proof of order
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)  # server clock
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    replay_status: Mapped[str] = mapped_column(String(28))
    replay_of_id: Mapped[str | None] = mapped_column(ForeignKey("sim_incoming_events.id"), nullable=True)
    __table_args__ = (
        Index("ix_sim_incoming_events_key", "project_id", "source", "event_key"),
        enum_check("source", EventSource, "ck_sim_incoming_events_source"),
        enum_check("event_type", EventType, "ck_sim_incoming_events_type"),
        enum_check("replay_status", ReplayStatus, "ck_sim_incoming_events_replay"),
        CheckConstraint("substr(source, 1, 10) = 'SIMULATED_'", name="ck_sim_incoming_events_simulated"),
        CheckConstraint("(replay_status = 'FIRST') = (replay_of_id IS NULL)", name="ck_sim_incoming_events_replay_link"),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="ck_sim_incoming_events_amount"),
        CheckConstraint(f"currency IS NULL OR ({_CURRENCY})", name="ck_sim_incoming_events_currency"),
    )


class SimInventoryMovement(Base):
    """Append-only stock movement. balance = initial_stock - SUM(RESERVE) - SUM(DEDUCT) + SUM(RELEASE). `operation_key` is the unique
    business operation identity: the same operation can be recorded once per project, however often it is attempted."""

    __tablename__ = "sim_inventory_movements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("sim_inventory_items.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("sim_orders.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    operation_key: Mapped[str] = mapped_column(String(100))
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("sim_incoming_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "operation_key", name="uq_sim_inventory_movements_project_operation"),
        enum_check("kind", MovementKind, "ck_sim_inventory_movements_kind"),
        CheckConstraint("quantity >= 1", name="ck_sim_inventory_movements_quantity"),
    )


make_append_only(SimInventoryMovement)


class SimAuditRecord(Base):
    """Append-only trail of every merchant-side effect and every processing attempt, including duplicates, refusals and the output of
    deliberately faulty fixtures (marked FAULTY_FIXTURE). A crash leaves NO record of itself: its absence is detectable as an event
    that was received but has no EVENT_PROCESSED record."""

    __tablename__ = "sim_audit_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("sim_incoming_events.id"), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    processor: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        enum_check("action", AuditAction, "ck_sim_audit_records_action"),
        enum_check("outcome", ProcessingOutcome, "ck_sim_audit_records_outcome", nullable=True),
        enum_check("processor", Processor, "ck_sim_audit_records_processor"),
        CheckConstraint("(action = 'EVENT_PROCESSED') = (outcome IS NOT NULL)", name="ck_sim_audit_records_outcome_link"),
    )


class SimRecoveryPlan(Base):
    """One immutable VERSION of a recovery plan for a divergence. Content (steps, evidence, reasons) is never rewritten: changed evidence
    produces a NEW version and the older one becomes superseded. `evidence_fingerprint` covers kind, payment, evidence and steps, and
    every approval is bound to (plan id, version, fingerprint)."""

    __tablename__ = "sim_recovery_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    plan_key: Mapped[str] = mapped_column(String(64))  # identity of the divergence: sha256(project, kind, payment)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payment_records.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(14))  # PlanStatus at birth
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON)  # why blocked / needs review, and manual follow-ups
    provider_fingerprint: Mapped[str] = mapped_column(String(64))  # provider-side facts only; re-verified at execution
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    created_by_seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "plan_key", "version", name="uq_sim_recovery_plans_key_version"),
        enum_check("kind", RecoveryKind, "ck_sim_recovery_plans_kind"),
        enum_check("status", PlanStatus, "ck_sim_recovery_plans_status"),
        CheckConstraint("version >= 1", name="ck_sim_recovery_plans_version"),
        CheckConstraint("(kind = 'FALSE_HEALTHY') = (payment_id IS NULL)", name="ck_sim_recovery_plans_payment_link"),
    )


make_append_only(SimRecoveryPlan)


class SimRecoveryEvent(Base):
    """Append-only history of a plan: creation, approval (who, when, exactly which version and fingerprint), every applied step, the
    single EXECUTED record, and every refusal. A plan's state is DERIVED from these rows, so no transition can be rewritten."""

    __tablename__ = "sim_recovery_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("sim_recovery_plans.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(20))
    actor_seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.id"))
    approved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # APPROVED / APPROVAL_REFUSED: the (attempted) binding
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("plan_id", "seq", name="uq_sim_recovery_events_plan_seq"),
        Index("uq_sim_recovery_events_one_approval", "plan_id", unique=True, sqlite_where=text("event_type = 'APPROVED'")),
        Index("uq_sim_recovery_events_one_execution", "plan_id", unique=True, sqlite_where=text("event_type = 'EXECUTED'")),
        enum_check("event_type", RecoveryEventType, "ck_sim_recovery_events_type"),
        CheckConstraint("seq >= 1", name="ck_sim_recovery_events_seq"),
        CheckConstraint("event_type != 'APPROVED' OR (approved_fingerprint IS NOT NULL AND approved_version IS NOT NULL)",
                        name="ck_sim_recovery_events_approval_binding"),
    )


for _model in (SimIncomingEvent, SimAuditRecord, SimRecoveryEvent):
    make_append_only(_model)

TRIGGERS: dict[type, list[tuple[str, str]]] = {
    SimInventoryItem: [],
    SimOrder: [same_project_trigger("sim_orders", "payment_id", "payment_records"), same_project_trigger("sim_orders", "item_id", "sim_inventory_items"),
               identity_trigger("sim_orders", ("project_id", "payment_id", "business_key", "item_id", "quantity", "amount_minor", "currency"))],
    SimFulfillment: [same_project_trigger("sim_fulfillments", "order_id", "sim_orders"),
                     identity_trigger("sim_fulfillments", ("project_id", "order_id", "operation_key")), dispatch_protection_trigger()],
    SimIncomingEvent: [same_project_trigger("sim_incoming_events", "payment_id", "payment_records"),
                       same_project_trigger("sim_incoming_events", "replay_of_id", "sim_incoming_events")],
    SimInventoryMovement: [same_project_trigger("sim_inventory_movements", "item_id", "sim_inventory_items"),
                           same_project_trigger("sim_inventory_movements", "order_id", "sim_orders"),
                           same_project_trigger("sim_inventory_movements", "source_event_id", "sim_incoming_events")],
    SimAuditRecord: [same_project_trigger("sim_audit_records", "event_id", "sim_incoming_events")],
    SimRecoveryPlan: [same_project_trigger("sim_recovery_plans", "payment_id", "payment_records")],
    SimRecoveryEvent: [same_project_trigger("sim_recovery_events", "plan_id", "sim_recovery_plans")],
}
for _model, _triggers in TRIGGERS.items():
    install_triggers(_model, _triggers)
