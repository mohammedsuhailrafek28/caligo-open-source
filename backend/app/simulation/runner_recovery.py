"""Runner steps 8-9, entirely offline: a SIMULATED merchant divergence on a PAID payment, then the existing guarded recovery flow.

The provider evidence is whatever the payment record holds (a real Sandbox read in an approved run, the fake in the rehearsal); the merchant
orders, stock and fulfillment are simulated (`sim_*`). The provenance label of the payment evidence must say which, and the checks fail if it does not.
"""
from sqlalchemy.orm import Session

from app import sandbox_attestation as attestation
from app.models import PaymentRecord
from app.simulation import scenarios, store

SKU = "SKU-RUN"
STOCK = 10


def _label_expected(payment_id: str) -> str:
    return attestation.LABEL_AUTHENTICATED if attestation.is_attested(payment_id) else attestation.LABEL_UNATTESTED


def recovery_checks(app, api, project_id: str, payment_id: str, out) -> bool:
    """Create the simulated divergence, then diagnose, plan, preview, approve and execute through the API. Returns True when every check passed."""
    with Session(app.state.engine, expire_on_commit=False) as db:
        item = store.add_inventory_item(db, project_id, SKU, STOCK)
        db.commit()
        scenarios.scenario_b_payment_success_merchant_failed(db, project_id, db.get(PaymentRecord, payment_id), item)
    root = f"/api/v1/projects/{project_id}/state-recovery"
    divergences = api.get(f"{root}/divergences").json()["divergences"]
    out.check("step 8: the simulated merchant divergence is detected on the provider-paid payment", any(d["payment_id"] == payment_id for d in divergences))
    diag = api.get(f"{root}/diagnosis", params={"kind": "PAID_NOT_APPLIED", "payment_id": payment_id}).json()
    payment_ev = next((e for e in diag["evidence"] if e["evidence_id"] == f"payment:{payment_id}"), None)
    out.check("step 8: the diagnosis is advisory and RULES_ONLY (no real LLM)", diag["advisory_only"] and diag["mode"] == "RULES_ONLY" and diag["llm_evaluation"] == "NOT_RUN")
    out.check(f"step 8: the provider evidence is labelled {_label_expected(payment_id)}", payment_ev is not None and payment_ev["provenance_label"] == _label_expected(payment_id))
    out.check("step 8: merchant evidence is labelled simulated", all(e["provenance_label"].startswith("SIMULATED_") for e in diag["evidence"] if e["source"] == "merchant"))
    plan = api.post(f"{root}/plans", {"kind": "PAID_NOT_APPLIED", "payment_id": payment_id}).json()
    preview = api.get(f"{root}/plans/{plan['id']}/preview").json()
    out.check("step 8: the preview makes no provider call and no financial change", preview["guarantees"] == {"provider_calls": 0, "financial_mutations": 0, "dispatched_shipments_reversed": 0})
    approved = api.post(f"{root}/plans/{plan['id']}/approve", {"version": plan["version"], "evidence_fingerprint": plan["evidence_fingerprint"]})
    done = api.post(f"{root}/plans/{plan['id']}/execute")
    events = [e["event_type"] for e in api.get(f"{root}/plans/{plan['id']}/events").json()["events"]]
    out.check("step 9: a human approval was recorded, then one guarded execution", approved.status_code == 200 and done.status_code == 200 and done.json()["executed_now"] is True)
    out.check("step 9: the audit trail holds the creation, one approval and one execution", events.count("APPROVED") == 1 and events.count("EXECUTED") == 1 and "PLAN_CREATED" in events)
    return all(passed for _, passed in out.checks)
