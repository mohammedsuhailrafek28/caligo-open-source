"""Citation validation for the unified report (R5). Pure read path.

A citation is (kind, id, payment_id). It resolves only if the record exists, belongs to THIS project, and - where the record is tied to a
payment and the citation names one - belongs to THAT payment. Project membership alone is not enough: a record of another payment in the
same project is reported as PAYMENT_MISMATCH. Problems are reported, never hidden and never auto-corrected."""
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BuyerAuthenticator, BuyerDecisionRecord, EvidenceArtifact, InstructionChangeRequest, PaymentEvent, PaymentInstruction,
    PaymentObservation, PaymentRecord, ProviderEvidenceGap, Requirement, ScopeBaseline, ScopeChange, ScopeRecord,
)

CHUNK = 500  # stays well under SQLite's bound-parameter limit
MAX_PROBLEMS_SHOWN = 50

Citation = tuple[str, str, str | None]  # (kind, id, payment_id the citing record is about, or None)

# kinds whose rows carry a payment_id; a citation naming a payment must match it.
_PAYMENT_BOUND = {"OBSERVATION": PaymentObservation, "EVIDENCE_GAP": ProviderEvidenceGap, "PAYMENT_EVENT": PaymentEvent}
_PROJECT_ONLY = {
    "PAYMENT_RECORD": PaymentRecord, "INSTRUCTION_VERSION": PaymentInstruction, "INSTRUCTION_CHANGE": InstructionChangeRequest,
    "AUTHENTICATOR": BuyerAuthenticator, "artifact": EvidenceArtifact, "change": ScopeChange, "buyer_decision": BuyerDecisionRecord,
    "baseline": ScopeBaseline, "scope_record": ScopeRecord, "requirement": Requirement, "payment": PaymentRecord,
}
_SCOPE_KINDS = frozenset({"artifact", "change", "buyer_decision", "baseline", "scope_record", "requirement", "payment", "project"})


def parse_ref(ref: str) -> tuple[str, str] | None:
    """`kind:id` strings used by the scope/delivery report. Anything else is returned as None (and reported as malformed)."""
    kind, sep, ident = str(ref).partition(":")
    return (kind, ident) if sep and kind in _SCOPE_KINDS and ident else None


def _chunks(items: list[str]):
    for i in range(0, len(items), CHUNK):
        yield items[i:i + CHUNK]


def _problem(kind: str, ident: str, reason: str) -> dict[str, Any]:
    return {"kind": kind, "id": ident, "reason": reason}  # ids only; stored free text is never echoed


def _existing(db: Session, project_id: str, kind: str, ids: list[str]) -> dict[str, str | None]:
    """id -> payment_id (None for kinds without one) for rows of THIS project."""
    found: dict[str, str | None] = {}
    if kind in _PAYMENT_BOUND:
        model = _PAYMENT_BOUND[kind]
        for chunk in _chunks(ids):
            found.update(db.execute(select(model.id, model.payment_id).where(model.project_id == project_id, model.id.in_(chunk))).all())
        return found
    model = _PROJECT_ONLY[kind]
    for chunk in _chunks(ids):
        found.update({i: i if kind in ("PAYMENT_RECORD", "payment") else None
                      for i in db.scalars(select(model.id).where(model.project_id == project_id, model.id.in_(chunk)))})
    return found


def validate(db: Session, project_id: str, citations: list[Citation]) -> dict[str, Any]:
    """Resolve every distinct citation. Returns counts and the (bounded) list of problems; `all_resolved` is False on any problem."""
    distinct = sorted({c for c in citations if c[1]}, key=lambda c: (c[0], c[1], c[2] or ""))
    problems: list[dict[str, Any]] = []
    by_kind: dict[str, list[str]] = {}
    for kind, ident, _ in distinct:
        if kind == "project":
            if ident != project_id:
                problems.append(_problem(kind, ident, "NOT_THIS_PROJECT"))
            continue
        if kind not in _PAYMENT_BOUND and kind not in _PROJECT_ONLY:
            problems.append(_problem(kind, ident, "UNKNOWN_CITATION_KIND"))
            continue
        by_kind.setdefault(kind, []).append(ident)
    found = {kind: _existing(db, project_id, kind, sorted(set(ids))) for kind, ids in by_kind.items()}
    for kind, ident, payment_id in distinct:
        if kind not in found:
            continue
        if ident not in found[kind]:
            problems.append(_problem(kind, ident, "NOT_FOUND_IN_PROJECT"))
        elif payment_id is not None and kind in (*_PAYMENT_BOUND, "PAYMENT_RECORD") and found[kind][ident] != payment_id:
            problems.append(_problem(kind, ident, "PAYMENT_MISMATCH"))
    return {"distinct_checked": len(distinct), "problem_count": len(problems), "all_resolved": not problems,
            "problems": problems[:MAX_PROBLEMS_SHOWN], "problems_truncated": len(problems) > MAX_PROBLEMS_SHOWN}
