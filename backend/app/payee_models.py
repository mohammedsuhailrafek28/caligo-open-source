"""Payee destination registry (adapted from the teammate's supplier registry).

A registry entry is the SELLER'S OWN RECORD of where a counterparty has said to pay: it is `SELLER_ASSERTED` provenance, never provider or buyer
evidence, and it is not an authorization of anything. A destination change is a NEW append-only version chained to its predecessor; nothing is
edited or deleted. The registry is advisory input for screening (`app.services.payee_registry.screen`): it cannot create, release or override
a payment hold, and the payment instruction (R1/R2) stays the only authority over where Caligo expects to be paid.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, UTCDateTime, make_append_only, monotonic_utcnow, new_id

PROVENANCE_SELLER_ASSERTED = "SELLER_ASSERTED"


class PayeeDestinationVersion(Base):
    __tablename__ = "payee_destination_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    payee_id: Mapped[str] = mapped_column(String(36), index=True)  # the id of the payee's first version; stable across versions
    version: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(120))
    destination: Mapped[str] = mapped_column(String(254))  # normalised (trimmed, lower-case) e-mail destination
    destination_sha256: Mapped[str] = mapped_column(String(64))
    previous_version_id: Mapped[str | None] = mapped_column(ForeignKey("payee_destination_versions.id"), nullable=True)
    chain_sha256: Mapped[str] = mapped_column(String(64))  # binds this version to its predecessor
    provenance: Mapped[str] = mapped_column(String(16), default=PROVENANCE_SELLER_ASSERTED)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "payee_id", "version", name="uq_payee_versions_project_payee_version"),
        CheckConstraint("version >= 1", name="ck_payee_versions_version"),
        CheckConstraint("provenance = 'SELLER_ASSERTED'", name="ck_payee_versions_provenance"),
        CheckConstraint("(version = 1) = (previous_version_id IS NULL)", name="ck_payee_versions_chain"),
    )


make_append_only(PayeeDestinationVersion)
