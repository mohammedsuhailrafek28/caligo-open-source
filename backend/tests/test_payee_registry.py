"""Payee destination registry (seller-asserted, append-only) and read-only advisory screening. Offline; never touches a provider, a hold or an instruction."""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from app.models import AppendOnlyError, AuditEvent, PaymentInstruction, PaymentRecord
from app.payee_models import PayeeDestinationVersion
from app.services import bec_signals
from tests.test_instruction_integrity import hold, record
from tests.test_payments import make_project

DEST = "supplier@example.org"


def root(pid):
    return f"/api/v1/projects/{pid}/payee-registry"


@pytest.fixture()
def pid(seller):
    return make_project(seller)


def register(seller, pid, label="Acme Supplies", destination=DEST, note=None):
    return seller.post(root(pid), json={"label": label, "destination": destination, "note": note})


def screen(seller, pid, **body):
    return seller.post(root(pid) + "/screen", json=body)


def codes(response):
    return [s["code"] for s in response.json()["signals"]]


def counts(app):
    with DbSession(app.state.engine) as db:
        return tuple(db.scalar(select(func.count()).select_from(m)) for m in (PayeeDestinationVersion, AuditEvent, PaymentInstruction, PaymentRecord))


# --- registry ---------------------------------------------------------------------------------------------------------------

def test_register_creates_a_seller_asserted_chained_version_one_and_audits_it(seller, pid):
    r = register(seller, pid, destination="  Supplier@Example.ORG ")
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert (v1["version"], v1["destination"], v1["provenance"]) == (1, DEST, "SELLER_ASSERTED")
    assert v1["payee_id"] == v1["id"] and v1["previous_version_id"] is None and len(v1["chain_sha256"]) == 64
    events = [e for e in seller.get(f"/api/v1/projects/{pid}/audit-events").json() if e["event_type"] == "PAYEE_REGISTERED"]
    assert len(events) == 1 and "supplier@" not in str(events[0]) and events[0]["detail"]["destination"] == "s***@example.org"


def test_a_destination_change_is_a_new_chained_version_and_never_an_edit(seller, pid):
    v1 = register(seller, pid).json()
    r = seller.post(f"{root(pid)}/versions".replace("/versions", f"/{v1['payee_id']}/versions"), json={"destination": "new@example.org", "note": "phoned them"})
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["previous_version_id"] == v1["id"] and v2["chain_sha256"] != v1["chain_sha256"]
    detail = seller.get(f"{root(pid)}/{v1['payee_id']}").json()
    assert [h["version"] for h in detail["history"]] == [2, 1] and detail["current"]["destination"] == "new@example.org"
    assert detail["independently_verified"] is False  # a seller's own record is never verification
    change = [e for e in seller.get(f"/api/v1/projects/{pid}/audit-events").json() if e["event_type"] == "PAYEE_DESTINATION_CHANGED"][0]
    assert change["detail"]["independently_verified"] is False and change["detail"]["previous_version"] == 1


def test_unchanged_duplicate_label_and_malformed_destinations_are_refused(seller, pid):
    v1 = register(seller, pid).json()
    assert seller.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": DEST.upper()}).status_code == 409
    assert register(seller, pid, label="acme supplies", destination="other@example.org").status_code == 409
    for bad in ("not-an-email", "a@b", "x" * 260 + "@example.org", "a b@example.org", "<script>@example.org"):
        assert register(seller, pid, label=f"L{abs(hash(bad))}", destination=bad).status_code in (409, 422), bad
        assert register(seller, pid, label=f"M{abs(hash(bad))}", destination=bad).status_code == 422


def test_history_is_append_only_in_the_orm_and_in_the_database(seller, pid, app):
    v1 = register(seller, pid).json()
    with DbSession(app.state.engine) as db:
        row = db.get(PayeeDestinationVersion, v1["id"])
        row.destination = "evil@example.org"
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("UPDATE payee_destination_versions SET destination = 'evil@example.org'"))
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("DELETE FROM payee_destination_versions"))


def test_registry_is_project_scoped_and_owner_only(seller, pid, seller_factory):
    v1 = register(seller, pid).json()
    other = seller_factory("intruder@example.org")
    other_pid = make_project(other)
    assert other.get(root(pid)).status_code == 404 and other.get(f"{root(pid)}/{v1['payee_id']}").status_code == 404
    assert other.post(root(pid), json={"label": "x", "destination": DEST}).status_code == 404
    assert other.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": "n@example.org"}).status_code == 404
    assert other.post(root(pid) + "/screen", json={"destination": DEST}).status_code == 404
    assert other.get(f"{root(other_pid)}/{v1['payee_id']}").status_code == 404  # another project's payee id is not found either
    assert screen(other, other_pid, payee_id=v1["payee_id"]).status_code == 404
    assert other.get(root(other_pid)).json() == []


def test_writes_need_a_session_and_csrf(seller, pid):
    assert seller.client.post(root(pid), json={"label": "x", "destination": DEST}).status_code == 403
    assert seller.client.__class__(seller.client.app).post(root(pid), json={"label": "x", "destination": DEST}).status_code == 401


def test_unknown_fields_are_rejected_so_trust_cannot_be_smuggled_in(seller, pid):
    for extra in ({"provenance": "provider_authenticated"}, {"verified": True}, {"project_id": "x"}, {"approved_by": "alice"}):
        assert seller.post(root(pid), json={"label": "x", "destination": DEST, **extra}).status_code == 422


# --- screening --------------------------------------------------------------------------------------------------------------

def test_screen_is_read_only_and_cannot_clear_a_hold(seller, pid, app):
    record(seller, pid, {"recipient_email": DEST, "currency": "USD", "amount_minor": 80000})
    v1 = register(seller, pid).json()
    before, hold_before = counts(app), hold(seller, pid)
    for _ in range(3):
        r = screen(seller, pid, payee_id=v1["payee_id"], destination="other@example.org", amount_minor=99999, text="URGENT: new bank details")
        assert r.status_code == 200 and r.json()["advisory_only"] is True and r.json()["human_review_required"] is True
        assert r.json()["outcome"] == "REVIEW_RECOMMENDED"
    assert counts(app) == before and hold(seller, pid) == hold_before
    assert "cleared" not in r.text.lower().replace("cannot approve, clear", "")


def test_a_matching_destination_yields_no_signals_but_never_an_approval(seller, pid):
    record(seller, pid, {"recipient_email": DEST, "currency": "USD", "amount_minor": 80000})
    v1 = register(seller, pid).json()
    r = screen(seller, pid, payee_id=v1["payee_id"], destination=DEST.upper(), amount_minor=80000, currency="USD")
    assert r.json()["outcome"] == "NO_SIGNALS_FOUND" and r.json()["signals"] == []
    assert not {"approved", "cleared", "verified"} & set(r.json()) - {"extracted"}


def test_destination_signals_cite_the_registry_version_and_the_instruction(seller, pid):
    inst = record(seller, pid, {"recipient_email": DEST, "currency": "USD", "amount_minor": 80000}).json()
    v1 = register(seller, pid).json()
    changed = seller.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": "new@example.org"}).json()
    r = screen(seller, pid, payee_id=v1["payee_id"], destination=DEST, amount_minor=1234, currency="EUR")
    assert r.status_code == 200, r.text
    got = {s["code"]: s for s in r.json()["signals"]}
    assert set(got) == {"DESTINATION_DIFFERS_FROM_REGISTRY", "DESTINATION_RECENTLY_CHANGED", "DESTINATION_SUPERSEDED", "AMOUNT_DIFFERS_FROM_INSTRUCTION",
                        "CURRENCY_DIFFERS_FROM_INSTRUCTION"}
    assert got["DESTINATION_DIFFERS_FROM_REGISTRY"]["cites"] == [f"payee:{v1['payee_id']}@v2"] and changed["version"] == 2
    assert got["DESTINATION_SUPERSEDED"]["cites"] == [f"payee:{v1['payee_id']}@v1"]
    assert got["AMOUNT_DIFFERS_FROM_INSTRUCTION"]["cites"] == [f"payment_instruction:{inst['id']}"]
    assert [s["severity"] for s in r.json()["signals"]] == sorted((s["severity"] for s in r.json()["signals"]), key={"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get)


def test_an_unregistered_destination_and_one_that_differs_from_the_instruction_are_flagged(seller, pid):
    record(seller, pid, {"recipient_email": DEST, "currency": "USD", "amount_minor": 80000})
    assert codes(screen(seller, pid, destination="stranger@example.org")) == ["DESTINATION_DIFFERS_FROM_INSTRUCTION", "DESTINATION_NOT_REGISTERED"]
    assert codes(screen(seller, pid, destination=DEST)) == ["DESTINATION_NOT_REGISTERED"]


def test_bec_wording_is_a_low_severity_hint_with_spans_and_the_text_is_never_echoed(seller, pid):
    text_ = "Hi, please pay immediately. We have new bank details, do not call to verify. Pay supplier@example.org"
    r = screen(seller, pid, text=text_)
    assert r.status_code == 200
    by = {s["code"]: s for s in r.json()["signals"]}
    assert {"URGENCY_LANGUAGE", "BANK_CHANGE_WORDING", "SECRECY_LANGUAGE"} <= set(by) and all(by[c]["severity"] == "LOW" for c in by)
    span = by["URGENCY_LANGUAGE"]["spans"][0]
    assert text_[span["start"]:span["end"]].lower() == "immediately"
    assert "new bank details" not in r.text and "do not call" not in r.text


def test_another_email_in_the_text_is_flagged_only_when_something_is_known(seller, pid):
    record(seller, pid, {"recipient_email": DEST, "currency": "USD", "amount_minor": 80000})
    r = screen(seller, pid, text="Invoice no: INV-2041. Total due: $800.00. Pay to attacker@evil.example")
    assert "TEXT_MENTIONS_OTHER_DESTINATION" in codes(r)
    x = r.json()["extracted"]
    assert x["verified"] is False and x["amount_minor"] == 80000 and x["currency"] == "USD" and x["invoice_reference"] == "INV-2041"
    assert x["emails"][0]["value"] == "attacker@evil.example"


@pytest.mark.parametrize("bad", ["x" * 4097, "a‮b", "a\x00b", ""])
def test_unsafe_or_oversize_text_is_rejected_without_echo(seller, pid, bad):
    r = screen(seller, pid, text=bad)
    assert r.status_code == 422 and "‮" not in r.text


def test_screen_needs_something_to_screen_and_rejects_trust_fields(seller, pid):
    assert seller.post(root(pid) + "/screen", json={}).status_code == 422
    assert screen(seller, pid, destination=DEST, verified=True).status_code == 422
    assert screen(seller, pid, payee_id="00000000-0000-0000-0000-000000000000").status_code == 404


def test_the_route_is_documented(app):
    paths = app.openapi()["paths"]
    assert set(paths["/api/v1/projects/{project_id}/payee-registry/screen"]) == {"post"}
    assert "/api/v1/projects/{project_id}/payee-registry/{payee_id}/versions" in paths


# --- pure extraction ----------------------------------------------------------------------------------------------------------

def test_extraction_never_guesses_and_parses_money_exactly():
    empty = bec_signals.extract_fields("Hello, thanks for the lovely meeting about Acme.")
    assert (empty.emails, empty.amount_minor, empty.currency, empty.invoice_reference) == ((), None, None, None)
    x = bec_signals.extract_fields("Amount due: EUR 1,234.50 for invoice #AB-77")
    assert (x.amount_minor, x.currency, x.invoice_reference) == (123450, "EUR", "AB-77")
    assert bec_signals.extract_fields("Total: 12.345").amount_minor is None  # sub-cent precision is not money
    assert bec_signals.extract_fields("Balance: 99999999999").amount_minor is None  # beyond the supported range


def test_phrase_matching_is_word_bounded():
    assert bec_signals.phrase_signals("Take a rushed lunch; the asapient urgencies") == ()
    assert [p.code for p in bec_signals.phrase_signals("URGENT")] == ["URGENCY_LANGUAGE"]


def test_a_concurrent_version_conflict_is_a_409_and_writes_nothing(seller, pid, app, monkeypatch):
    from app.services import payee_registry
    v1 = register(seller, pid).json()
    original = payee_registry.versions_of
    calls = {"n": 0}

    def stale(db, project_id, payee_id):  # the first change reads, then another writer takes version 2 before our flush
        rows = original(db, project_id, payee_id)
        calls["n"] += 1
        if calls["n"] == 1:
            with DbSession(app.state.engine) as other:
                prev = rows[-1]
                other.add(PayeeDestinationVersion(project_id=project_id, payee_id=payee_id, version=2, label=prev.label, destination="raced@example.org",
                                                  destination_sha256="0" * 64, previous_version_id=prev.id, chain_sha256="1" * 64,
                                                  created_by_seller_id=prev.created_by_seller_id))
                other.commit()
        return rows

    monkeypatch.setattr(payee_registry, "versions_of", stale)
    r = seller.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": "mine@example.org"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict", r.text
    monkeypatch.undo()
    assert [h["destination"] for h in seller.get(f"{root(pid)}/{v1['payee_id']}").json()["history"]] == ["raced@example.org", DEST]


def test_versions_per_payee_are_capped(seller, pid, monkeypatch):
    from app.services import payee_registry
    monkeypatch.setattr(payee_registry, "MAX_VERSIONS_PER_PAYEE", 2)
    v1 = register(seller, pid).json()
    assert seller.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": "b@example.org"}).status_code == 201
    r = seller.post(f"{root(pid)}/{v1['payee_id']}/versions", json={"destination": "c@example.org"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_limit"


def test_whitespace_padding_cannot_make_amount_extraction_slow():
    import time
    start = time.perf_counter()
    bec_signals.extract_fields("total" + " " * 4000 + "x")
    bec_signals.extract_fields(("total " + " " * 40) * 80)
    assert time.perf_counter() - start < 0.5


def test_oversize_text_validation_error_does_not_echo_the_text(seller, pid):
    r = screen(seller, pid, text="Q" * 4097)
    assert r.status_code == 422 and "QQQQQQQQ" not in r.text
