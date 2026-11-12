"""Deterministic comparison of merchant/seller claims with provider observations (R3). Read-only and advisory.

`compare_payment` is a pure function of a payment record and its observations: the same inputs always give the same findings in the
same order, each citing the observations (id, source, confidence, payload hash) it rests on, so a later explanation engine (R4) can
quote them. The comparison NEVER writes: it cannot change a status, verify a payment or release a hold. Reconciliation
(`reconcile.evaluate/apply`) stays the only authority on payment state."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas
from app.models import PaymentObservation, PaymentRecord, ProviderEvidenceGap
from app.schemas import FindingCode as F, FindingSeverity as Sev, ObservationKind as K, ObservationSource as S
from app.services.scope import canonical_sha256

DELAY_THRESHOLD = timedelta(hours=24)
FUTURE_SKEW = timedelta(minutes=5)
MAX_CITATIONS = 20  # per finding; the full count is reported in `citations_total`
MAX_OBSERVATIONS_PER_PAYMENT = 2000  # newest N are compared; older ones are reported as truncated, never silently ignored
MAX_PAYMENTS_PER_PAGE = 50
MAX_PROVIDER_ROWS_PER_PAYMENT = 5000  # provider rows are bounded by what the provider reports, not by the seller
MAX_FINDINGS_PER_PAYMENT = 200
AGGREGATE_ABOVE = 5  # more than this many findings of one claim-derived kind collapse into one (counts and citations are kept)
# Claim-derived finding kinds: one per claim (or claim group), so a flood of claims could otherwise crowd out everything else.
PER_CLAIM_CODES = frozenset({
    F.CLAIM_CORROBORATED, F.CLAIM_AMOUNT_MISMATCH, F.CLAIM_CURRENCY_MISMATCH, F.CLAIM_NOT_IN_PROVIDER_EVIDENCE,
    F.CLAIM_AWAITING_PROVIDER_EVIDENCE, F.CLAIM_WITHOUT_TRANSACTION_REFERENCE, F.CLAIM_TIMESTAMP_IN_FUTURE, F.DELAYED_OBSERVATION,
    F.OUT_OF_ORDER_OBSERVATION, F.CONFLICTING_CLAIMS, F.DUPLICATE_CLAIM, F.CLAIM_MATCHES_EXTERNAL_RECORD, F.CLAIM_PROVIDER_EVIDENCE_INCOMPLETE})
_SEVERITY_RANK = {Sev.CONFLICT: 0, Sev.WARNING: 1, Sev.INFO: 2}
# Lifecycle order of provider invoice states; a later reading with a lower rank (or a settled invoice later "cancelled") is a regression.
_RANK = {"DRAFT": 0, "SENT": 1, "SCHEDULED": 1, "UNPAID": 1, "PAYMENT_PENDING": 2, "PARTIALLY_PAID": 3, "MARKED_AS_PAID": 4, "PAID": 4,
         "PARTIALLY_REFUNDED": 5, "MARKED_AS_REFUNDED": 6, "REFUNDED": 6}
_SETTLED = frozenset({"PARTIALLY_PAID", "MARKED_AS_PAID", "PAID", "PARTIALLY_REFUNDED", "MARKED_AS_REFUNDED", "REFUNDED"})
_CLAIMS = (S.MERCHANT_IMPORT.value, S.SELLER_ENTRY.value, S.WEBHOOK.value)


def _cite(o: PaymentObservation) -> schemas.ObservationCitation:
    return schemas.ObservationCitation(observation_id=o.id, source=S(o.source), confidence=schemas.ObservationConfidence(o.confidence),
                                       payload_sha256=o.payload_sha256)


def _finding(payment_id: str, code: F, severity: Sev, summary: str, *observations: PaymentObservation,
             gap_ids: tuple[str, ...] = ()) -> schemas.ObservationFinding:
    cited = sorted({o.id: o for o in observations}.values(), key=lambda o: (o.observed_at, o.id))
    shown = cited if len(cited) <= MAX_CITATIONS else cited[:MAX_CITATIONS // 2] + cited[-(MAX_CITATIONS // 2):]  # oldest AND newest
    return schemas.ObservationFinding(code=code, severity=severity, summary=summary, payment_id=payment_id,
                                      citations=[_cite(o) for o in shown], citations_total=len(cited),
                                      gap_ids=sorted(gap_ids)[:MAX_CITATIONS])


def _distinct_claim_total(claims: list[PaymentObservation]) -> int:
    """Claimed money counted once per (transaction reference) or, with no reference, once per identical line."""
    seen: dict[tuple, int] = {}
    for c in claims:
        seen.setdefault(("txn", c.source_event_id) if c.source_event_id else ("line", c.payload_sha256, c.source_reference), c.amount_minor)
    return sum(seen.values())


def _aggregate(findings: list[schemas.ObservationFinding]) -> list[schemas.ObservationFinding]:
    """Collapse repetitive per-claim findings into one per (code, severity), keeping the count and every citation (shown oldest-id first,
    capped; `citations_total` is the real number). Provider-derived findings are never aggregated."""
    groups: dict[tuple, list[schemas.ObservationFinding]] = {}
    for f in findings:
        groups.setdefault((f.code, f.severity), []).append(f)
    result: list[schemas.ObservationFinding] = []
    for (code, severity), group in groups.items():
        if code not in PER_CLAIM_CODES or len(group) <= AGGREGATE_ABOVE:
            result.extend(group)
            continue
        group = sorted(group, key=lambda f: ([c.observation_id for c in f.citations], f.summary))
        cites = {c.observation_id: c for f in group for c in f.citations}
        ordered = [cites[k] for k in sorted(cites)]
        shown = ordered if len(ordered) <= MAX_CITATIONS else ordered[:MAX_CITATIONS // 2] + ordered[-(MAX_CITATIONS // 2):]
        hidden = sum(max(0, f.citations_total - len(f.citations)) for f in group)
        result.append(schemas.ObservationFinding(
            code=code, severity=severity, payment_id=group[0].payment_id, citations=shown, citations_total=len(ordered) + hidden,
            gap_ids=sorted({g for f in group for g in f.gap_ids})[:MAX_CITATIONS],
            summary=f"{len(group)} findings of this kind (first: {group[0].summary})"))
    return result


def _duplicate_summary(group: list[PaymentObservation]) -> str:
    sources = sorted({c.source for c in group})
    imports = len({c.import_id for c in group if c.import_id})
    return (f"{len(group)} separate submissions carry the same claim (sources: {', '.join(sources)}"
            f"{f'; {imports} import file(s)' if imports else ''}). Each was kept with its own provenance; "
            "a claim without a transaction reference counts once in the total unless it carries its own claim_id.")


def compare_payment(rec: PaymentRecord, observations: list[PaymentObservation], *, ledger_present: bool,
                    gaps: list[ProviderEvidenceGap] | None = None, truncated: bool = False,
                    provider_truncated: bool = False) -> schemas.PaymentComparison:
    pid = rec.id
    gaps = gaps or []
    incomplete = bool(gaps) or provider_truncated
    ordered = sorted(observations, key=lambda o: (o.observed_at, o.id))
    provider = [o for o in ordered if o.source == S.PROVIDER_API.value]
    claims = [o for o in ordered if o.source in _CLAIMS and o.kind in (K.PAYMENT.value, K.REFUND.value)]
    states = [o for o in provider if o.kind == K.INVOICE_STATE.value]
    all_money = [o for o in provider if o.kind in (K.PAYMENT.value, K.REFUND.value)]
    # Only the NEWEST reading of each provider transaction counts: an earlier reading with another amount is history, not money.
    latest_by_txn: dict[tuple, PaymentObservation] = {}
    history: dict[tuple, list[PaymentObservation]] = {}
    for o in all_money:  # ordered oldest to newest
        key = (o.kind, o.source_event_id or o.id)
        latest_by_txn[key] = o
        history.setdefault(key, []).append(o)
    provider_by_key = {k: o for k, o in latest_by_txn.items() if o.source_event_id}
    provider_money = list(latest_by_txn.values())
    out: list[schemas.ObservationFinding] = []
    for group in history.values():
        if len({o.payload_sha256 for o in group}) > 1:
            out.append(_finding(pid, F.PROVIDER_TRANSACTION_CHANGED, Sev.WARNING,
                                "The provider reported this transaction differently at different times; the newest reading is used.", *group))

    for c in claims:
        match = provider_by_key.get((c.kind, c.source_event_id)) if c.source_event_id else None
        if c.currency != rec.currency:
            out.append(_finding(pid, F.CLAIM_CURRENCY_MISMATCH, Sev.CONFLICT, f"The claim is in {c.currency}; the invoice is in {rec.currency}.", c))
        if match is not None:
            if match.currency != c.currency and c.currency == rec.currency:
                out.append(_finding(pid, F.CLAIM_CURRENCY_MISMATCH, Sev.CONFLICT, "The claim's currency differs from the provider's record of the same transaction.", c, match))
            if match.amount_minor != c.amount_minor:
                out.append(_finding(pid, F.CLAIM_AMOUNT_MISMATCH, Sev.CONFLICT,
                                    f"The claim says {c.amount_minor} minor units; the provider reported {match.amount_minor} for the same transaction.", c, match))
            elif match.instrument == "EXTERNAL":  # a record the seller can make in PayPal is not independent corroboration
                out.append(_finding(pid, F.CLAIM_MATCHES_EXTERNAL_RECORD, Sev.WARNING,
                                    "The claim matches a payment recorded outside PayPal, which is not proof of a PayPal payment.", c, match))
            elif match.currency == c.currency:
                out.append(_finding(pid, F.CLAIM_CORROBORATED, Sev.INFO, "The provider reported the same transaction, amount and currency.", c, match))
        elif not c.source_event_id:
            out.append(_finding(pid, F.CLAIM_WITHOUT_TRANSACTION_REFERENCE, Sev.INFO, "The claim has no transaction reference, so it can only be compared in total.", c))
        elif incomplete:  # the provider's evidence is known to be partial: absence proves nothing
            out.append(_finding(pid, F.CLAIM_PROVIDER_EVIDENCE_INCOMPLETE, Sev.WARNING,
                                "The stored provider evidence for this payment is incomplete (values that could not be stored, or older "
                                "provider observations left out), so this claim can be neither corroborated nor contradicted by it.",
                                c, gap_ids=tuple(g.id for g in gaps)))
        elif states:
            out.append(_finding(pid, F.CLAIM_NOT_IN_PROVIDER_EVIDENCE, Sev.WARNING,
                                "The provider's evidence for this invoice does not contain this transaction.", c, *states[-1:]))
        else:
            out.append(_finding(pid, F.CLAIM_AWAITING_PROVIDER_EVIDENCE, Sev.INFO, "The provider has not been synchronized yet, so nothing can corroborate this claim.", c))
        if c.asserted_at is not None:
            if c.asserted_at > c.observed_at + FUTURE_SKEW:
                out.append(_finding(pid, F.CLAIM_TIMESTAMP_IN_FUTURE, Sev.WARNING, "The claim's own timestamp is later than the time Caligo recorded it.", c))
            elif c.observed_at - c.asserted_at > DELAY_THRESHOLD:
                out.append(_finding(pid, F.DELAYED_OBSERVATION, Sev.INFO, "The claim was recorded more than 24 hours after the time it asserts.", c))

    by_ref: dict[tuple, list[PaymentObservation]] = {}
    for c in claims:
        if c.source_event_id:
            by_ref.setdefault((c.kind, c.source_event_id), []).append(c)
    for group in by_ref.values():
        if len({c.payload_sha256 for c in group}) > 1:
            out.append(_finding(pid, F.CONFLICTING_CLAIMS, Sev.CONFLICT, "Claims for the same transaction disagree with each other.", *group))
        elif len(group) > 1:
            out.append(_finding(pid, F.DUPLICATE_CLAIM, Sev.INFO, _duplicate_summary(group), *group))
    by_line: dict[tuple, list[PaymentObservation]] = {}
    for c in claims:
        if not c.source_event_id:
            by_line.setdefault((c.kind, c.payload_sha256), []).append(c)
    for group in by_line.values():
        if len(group) > 1:
            out.append(_finding(pid, F.DUPLICATE_CLAIM, Sev.INFO, _duplicate_summary(group), *group))

    stamped = [c for c in claims if c.asserted_at is not None]
    latest = None
    for c in stamped:  # in the order Caligo received them
        if latest is not None and c.asserted_at < latest.asserted_at:
            out.append(_finding(pid, F.OUT_OF_ORDER_OBSERVATION, Sev.INFO, "This claim was received after a claim that asserts a later time.", latest, c))
        elif latest is None or c.asserted_at > latest.asserted_at:
            latest = c

    previous = None
    for st in states:
        if previous is not None:
            was, now = previous.provider_state, st.provider_state
            if (now in _RANK and was in _RANK and _RANK[now] < _RANK[was]) or (was in _SETTLED and now == "CANCELLED"):
                out.append(_finding(pid, F.PROVIDER_STATE_REGRESSION, Sev.CONFLICT, f"The provider's invoice state moved from {was} to {now}.", previous, st))
        previous = st

    for o in provider_money:
        if o.instrument == "EXTERNAL":
            out.append(_finding(pid, F.PROVIDER_REPORTS_EXTERNAL_PAYMENT, Sev.WARNING, "The provider lists a payment recorded outside PayPal; it is not proof of a PayPal payment.", o))
    if ledger_present and not truncated:  # with older claims left out, 'no claim mentions it' cannot be asserted
        claimed_refs = {(c.kind, c.source_event_id) for c in claims if c.source_event_id}
        for o in provider_money:
            if o.instrument == "PAYPAL" and o.source_event_id and (o.kind, o.source_event_id) not in claimed_refs:
                out.append(_finding(pid, F.PROVIDER_PAYMENT_NOT_IN_LEDGER, Sev.INFO, "The provider reported a transaction that no ledger claim mentions.", o))

    claimed_pay = [c for c in claims if c.kind == K.PAYMENT.value and c.currency == rec.currency]
    claimed_ref = [c for c in claims if c.kind == K.REFUND.value and c.currency == rec.currency]
    claimed_paid, claimed_refunded = _distinct_claim_total(claimed_pay), _distinct_claim_total(claimed_ref)
    provider_paid = sum(o.amount_minor for o in provider_money if o.kind == K.PAYMENT.value and o.instrument == "PAYPAL" and o.currency == rec.currency)
    if claimed_paid > rec.amount_minor:
        out.append(_finding(pid, F.CLAIMED_PAYMENTS_EXCEED_INVOICE, Sev.CONFLICT, f"Claimed payments total {claimed_paid}; the invoice is for {rec.amount_minor}.", *claimed_pay))
    if claimed_refunded > claimed_paid and not truncated:  # with older payment claims left out the comparison would be a false alarm
        out.append(_finding(pid, F.CLAIMED_REFUNDS_EXCEED_PAYMENTS, Sev.CONFLICT, "Claimed refunds exceed claimed payments.", *claimed_ref, *claimed_pay))
    if gaps:
        out.append(_finding(pid, F.PROVIDER_EVIDENCE_INCOMPLETE, Sev.WARNING,
                            f"{len(gaps)} provider value(s) could not be stored ({', '.join(sorted({g.reason for g in gaps}))}). "
                            "The stored provider evidence is incomplete: a missing observation is not evidence that a payment did not occur.",
                            gap_ids=tuple(g.id for g in gaps)))
    if truncated or provider_truncated:
        out.append(_finding(pid, F.COMPARISON_INPUT_TRUNCATED, Sev.WARNING,
                            f"Only the newest {MAX_OBSERVATIONS_PER_PAYMENT} claims and {MAX_PROVIDER_ROWS_PER_PAYMENT} provider observations "
                            "of this payment were compared; older ones were left out."))
    if claims and states and claimed_paid != provider_paid and not incomplete and not truncated:
        out.append(_finding(pid, F.CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER, Sev.WARNING,
                            f"Claimed payments total {claimed_paid}; PayPal payments the provider reported total {provider_paid}.",
                            *claimed_pay, *[o for o in provider_money if o.kind == K.PAYMENT.value][:20]))

    # Severity first (CONFLICT, WARNING, INFO); within a severity provider-derived findings come before claim-derived ones, so no
    # number of claims can push gaps, state regressions or external-payment warnings out of the shown findings.
    out = _aggregate(out)
    out.sort(key=lambda f: (_SEVERITY_RANK[f.severity], f.code in PER_CLAIM_CODES, f.code.value,
                            [c.observation_id for c in f.citations], f.summary))
    if len(out) > MAX_FINDINGS_PER_PAYMENT:
        out = out[:MAX_FINDINGS_PER_PAYMENT]
        out.append(_finding(pid, F.COMPARISON_INPUT_TRUNCATED, Sev.WARNING, f"Only the first {MAX_FINDINGS_PER_PAYMENT} findings are shown."))
        truncated = True
    return schemas.PaymentComparison(
        payment_id=pid, invoice_number=rec.invoice_number, authoritative_status=schemas.PaymentProviderStatus(rec.provider_status),
        provider_paid_minor=provider_paid, claimed_paid_minor=claimed_paid, findings=out,
        provider_evidence_complete=not incomplete, evidence_gap_count=len(gaps), claims_truncated=truncated)


def compare_project(db: Session, project_id: str, *, limit: int = 25, offset: int = 0,
                    payment_id: str | None = None) -> schemas.ObservationComparison:
    """One bounded page of payments (stable order) with bounded work per payment. The result is a pure function of the stored rows."""
    limit = max(1, min(limit, MAX_PAYMENTS_PER_PAGE))
    offset = max(0, offset)
    scope = [PaymentRecord.project_id == project_id, *([PaymentRecord.id == payment_id] if payment_id else [])]
    total = db.scalar(select(func.count()).select_from(PaymentRecord).where(*scope)) or 0
    payments = list(db.scalars(select(PaymentRecord).where(*scope).order_by(PaymentRecord.created_at, PaymentRecord.id).limit(limit).offset(offset)))
    ledger_present = db.scalar(select(PaymentObservation.id).where(
        PaymentObservation.project_id == project_id, PaymentObservation.source == S.MERCHANT_IMPORT.value).limit(1)) is not None
    gaps_by_payment: dict[str, list[ProviderEvidenceGap]] = {}
    if payments:
        for gap in db.scalars(select(ProviderEvidenceGap).where(ProviderEvidenceGap.project_id == project_id,
                                                                ProviderEvidenceGap.payment_id.in_([p.id for p in payments]))
                              .order_by(ProviderEvidenceGap.observed_at, ProviderEvidenceGap.id)):
            gaps_by_payment.setdefault(gap.payment_id, []).append(gap)
    results = []
    for payment in payments:
        def newest(is_provider: bool, cap: int) -> list[PaymentObservation]:
            test = PaymentObservation.source == S.PROVIDER_API.value
            return list(db.scalars(select(PaymentObservation).where(
                PaymentObservation.project_id == project_id, PaymentObservation.payment_id == payment.id, test if is_provider else ~test)
                .order_by(PaymentObservation.observed_at.desc(), PaymentObservation.id.desc()).limit(cap + 1)))

        # Provider evidence is loaded separately so a flood of seller claims can never push it out of the window.
        provider_rows, claim_rows = newest(True, MAX_PROVIDER_ROWS_PER_PAYMENT), newest(False, MAX_OBSERVATIONS_PER_PAYMENT)
        results.append(compare_payment(
            payment, provider_rows[:MAX_PROVIDER_ROWS_PER_PAYMENT] + claim_rows[:MAX_OBSERVATIONS_PER_PAYMENT], ledger_present=ledger_present,
            gaps=gaps_by_payment.get(payment.id, []), truncated=len(claim_rows) > MAX_OBSERVATIONS_PER_PAYMENT,
            provider_truncated=len(provider_rows) > MAX_PROVIDER_ROWS_PER_PAYMENT))
    snapshot = canonical_sha256({"offset": offset, "limit": limit, "total": total,
                                 "findings": [[f.model_dump(mode="json") for f in r.findings] for r in results]})
    return schemas.ObservationComparison(project_id=project_id, payments=results, total_payments=total, offset=offset, limit=limit,
                                         comparison_sha256=snapshot)
