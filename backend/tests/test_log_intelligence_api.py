"""Read-only, authenticated, project-scoped log-intelligence endpoint. Offline; the Stanza model is never loaded here (a test double stands in)."""
import pytest

from app.log_intelligence import LogIntelligenceService, integration
from tests.test_payment_observations import paid, unpaid  # noqa: F401 (fixtures)
from tests.test_s4_integration import base, counts, provider_snapshot
from tests.test_state_recovery import scenario_a  # noqa: F401
from tests.test_state_simulation import STOCK, deliver, orders, refund_at_provider, run, sim  # noqa: F401 (fixtures)

LINES = [{"text": "Payment captured; merchant database failed."}, {"text": "The order timed out while the workers were failing."}]


def url(sim):
    return f"{base(sim)}/log-intelligence"


def body(sim, **over):
    return {"kind": "DUPLICATE_ORDERS", "payment_id": sim.payment.id, "logs": LINES, **over}


class FakeProcessor:  # test double only; never presented as a measured Stanza result
    def analyze(self, text):
        return (("capture", 8, 16), ("fail", 36, 42))


def test_default_is_deterministic_cited_and_advisory(sim):
    scenario_a(sim)
    r = sim.seller.post(url(sim), json=body(sim))
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["requested_mode"], out["effective_mode"], out["fallback_reason"]) == ("deterministic", "deterministic", None)
    assert out["advisory_only"] is True and out["report"]["human_review_required"] is True
    assert out["log_lines_analyzed"] == 2 and out["kind"] == "DUPLICATE_ORDERS" and out["payment_id"] == sim.payment.id
    refs = {e["evidence_id"] for e in out["evidence"]}
    assert any(x.startswith("order:") for x in refs)
    assert {s["evidence_ref"] for s in out["log_sources"]} == {"log:0", "log:1"}
    assert all(s["provenance"] == "untrusted_log" for s in out["log_sources"])
    cited = {c["evidence_ref"] for c in out["report"]["extraction"]["clues"]} | {c["evidence_ref"] for c in out["report"]["extraction"]["exact_candidates"]}
    assert cited and cited <= {"log:0", "log:1"}
    assert "Payment captured" not in r.text  # raw log text is never echoed


def test_the_rules_diagnosis_is_the_existing_one_and_logs_cannot_invent_facts(sim):
    scenario_a(sim)
    plain = sim.seller.get(f"{base(sim)}/diagnosis", params={"kind": "DUPLICATE_ORDERS", "payment_id": sim.payment.id}).json()["diagnosis"]
    lying = [{"text": "transaction id TXN123456 amount 99.00 USD status paid and refunded"}]
    out = sim.seller.post(url(sim), json=body(sim, logs=lying)).json()
    assert out["report"]["rules_diagnosis"] == plain
    assert out["report"]["extraction"]["exact_candidates"] and all(c["verified"] is False for c in out["report"]["extraction"]["exact_candidates"])


def test_it_writes_nothing_and_calls_no_provider(sim):
    scenario_a(sim)
    before, snap, n_orders = counts(sim), provider_snapshot(sim), len(orders(sim))
    for _ in range(3):
        assert sim.seller.post(url(sim), json=body(sim)).status_code == 200
    assert counts(sim) == before and provider_snapshot(sim) == snap and len(orders(sim)) == n_orders


def test_stanza_without_a_configured_model_falls_back_visibly(sim):
    scenario_a(sim)
    out = sim.seller.post(url(sim), json=body(sim, requested_mode="stanza")).json()
    assert (out["requested_mode"], out["effective_mode"], out["fallback_reason"]) == ("stanza", "deterministic", "model_not_configured")
    assert {c["basis"] for c in out["report"]["extraction"]["clues"]} == {"surface_rule"}


def test_stanza_with_a_configured_model_and_low_memory_falls_back(sim, monkeypatch, tmp_path):
    scenario_a(sim)
    sim.app.state.settings = sim.app.state.settings.model_copy(update={"stanza_model_dir": str(tmp_path)})
    monkeypatch.setattr(integration, "_available_memory", lambda: 1)
    out = sim.seller.post(url(sim), json=body(sim, requested_mode="stanza")).json()
    assert (out["effective_mode"], out["fallback_reason"]) == ("deterministic", "insufficient_memory")
    assert str(tmp_path) not in str(out)  # the server path never leaks


def test_stanza_with_a_configured_model_uses_it_and_marks_the_basis(sim, monkeypatch, tmp_path):
    scenario_a(sim)
    sim.app.state.settings = sim.app.state.settings.model_copy(update={"stanza_model_dir": str(tmp_path)})
    monkeypatch.setattr(integration, "_available_memory", lambda: 2 * 1024**3)
    monkeypatch.setattr(LogIntelligenceService, "_load_processor", lambda self: FakeProcessor())
    out = sim.seller.post(url(sim), json=body(sim, logs=[{"text": "Payment captured; merchant database failed."}], requested_mode="stanza")).json()
    assert (out["requested_mode"], out["effective_mode"], out["fallback_reason"]) == ("stanza", "stanza", None)
    assert out["advisory_only"] is True and out["report"]["human_review_required"] is True
    assert "stanza_lemma" in {c["basis"] for c in out["report"]["extraction"]["clues"]}


def test_a_failing_model_never_breaks_the_request(sim, monkeypatch, tmp_path):
    scenario_a(sim)
    sim.app.state.settings = sim.app.state.settings.model_copy(update={"stanza_model_dir": str(tmp_path)})
    monkeypatch.setattr(integration, "_available_memory", lambda: 2 * 1024**3)

    def boom(self):
        raise RuntimeError("private detail")

    monkeypatch.setattr(LogIntelligenceService, "_load_processor", boom)
    r = sim.seller.post(url(sim), json=body(sim, requested_mode="stanza"))
    assert r.status_code == 200 and r.json()["fallback_reason"] == "model_unavailable" and "private detail" not in r.text


def test_project_wide_health_scope_needs_no_payment(sim):
    r = sim.seller.post(url(sim), json={"kind": "FALSE_HEALTHY", "logs": LINES})
    assert r.status_code == 200, r.text
    assert r.json()["payment_id"] is None and r.json()["report"]["extraction"]["payment_id"] == f"project:{sim.pid}"


@pytest.mark.parametrize("bad", [
    {"logs": []},
    {"logs": [{"text": "x"}] * 101},
    {"logs": [{"text": "x" * 4097}]},
    {"logs": [{"text": "the password is hunter2"}]},
    {"logs": [{"text": "line", "evidence_ref": "order:1"}]},
    {"project_id": "other"},
    {"provenance": "provider_authenticated"},
    {"model_dir": "C:/anywhere"},
    {"requested_mode": "llm"},
])
def test_unbounded_unsafe_or_caller_trusted_input_is_rejected(sim, bad):
    scenario_a(sim)
    before = counts(sim)
    r = sim.seller.post(url(sim), json=body(sim, **bad))
    assert r.status_code == 422, r.text
    assert "hunter2" not in r.text and counts(sim) == before


def test_another_seller_and_anonymous_callers_are_refused(sim, seller_factory):
    scenario_a(sim)
    assert seller_factory("intruder@example.com").post(url(sim), json=body(sim)).status_code == 404
    assert sim.seller.client.__class__(sim.app).post(url(sim), json=body(sim)).status_code == 401


def test_a_payment_of_another_project_is_not_found(sim):
    scenario_a(sim)
    assert sim.seller.post(url(sim), json=body(sim, payment_id="00000000-0000-0000-0000-000000000000")).status_code == 404


def test_csrf_is_required_because_the_route_is_a_post(sim):
    scenario_a(sim)
    assert sim.seller.client.post(url(sim), json=body(sim)).status_code == 403


def test_the_route_is_documented_and_post_only(sim):
    spec = sim.app.openapi()["paths"][f"/api/v1/projects/{{project_id}}/state-recovery/log-intelligence"]
    assert set(spec) == {"post"}


@pytest.mark.skipif(not __import__("os").environ.get("CALIGO_STANZA_TEST_CACHE"), reason="needs the local verified Stanza cache (CALIGO_STANZA_TEST_CACHE)")
def test_real_stanza_model_through_the_endpoint(sim):
    scenario_a(sim)
    sim.app.state.settings = sim.app.state.settings.model_copy(update={"stanza_model_dir": __import__("os").environ["CALIGO_STANZA_TEST_CACHE"]})
    plain = sim.seller.post(url(sim), json=body(sim)).json()
    out = sim.seller.post(url(sim), json=body(sim, requested_mode="stanza")).json()
    assert (out["requested_mode"], out["effective_mode"], out["fallback_reason"]) == ("stanza", "stanza", None)
    assert out["report"]["rules_diagnosis"] == plain["report"]["rules_diagnosis"] and out["advisory_only"] is True
    assert "stanza_lemma" in {c["basis"] for c in out["report"]["extraction"]["clues"]}
