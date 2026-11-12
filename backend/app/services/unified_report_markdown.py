"""Deterministic Markdown rendering of the unified evidence report. Every stored string is escaped (no raw HTML, no table breakouts);
the report hash printed here is the JSON report's `report_sha256`, so the two representations can be checked against each other."""
from typing import Any

from app.services.report_markdown import esc as _esc

_NONE = "_None._"


def esc(value: Any) -> str:
    """Escape for Markdown AND defuse GitHub-style autolinks (bare URLs, www., e-mail addresses) in stored text."""
    return _esc(value).replace("://", "&#58;//").replace("www.", "www&#46;").replace("@", "&#64;")


def code(value: Any) -> str:
    """Content for a code span: backticks removed so the span cannot be closed early."""
    return " ".join(str(value if value is not None else "").replace("`", "'").split())


def num(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return esc(value)


def _list(items: list, line) -> list[str]:
    return [line(i) for i in items] if items else [_NONE]


def render_unified_markdown(r: dict[str, Any]) -> str:
    lines = [f"# Unified evidence report: {esc(r['project']['title'])}", "",
             f"Project: `{code(r['project']['id'])}` · report version {esc(r['report_version'])}", f"Report SHA-256: `{code(r['report_sha256'])}`", ""]
    for section in (_classes, _summary, _scope, _instructions, _authorizations, _payments, _gaps, _cases, _citations, _truncation, _limitations):
        lines += section(r)
    return "\n".join(lines) + "\n"


def _classes(r: dict) -> list[str]:
    return ["## How to read this report", "", "Every statement carries an evidence class. They are never interchangeable.", ""] + [
        f"- **{esc(k)}**: {esc(v)}" for k, v in r["evidence_classes"].items()] + [""]


def _summary(r: dict) -> list[str]:
    s = r["summary"]
    incomplete = ", ".join(f"`{code(p)}`" for p in s["payments_with_incomplete_provider_evidence"]) or "none"
    return ["## Summary", "", f"- Payments on this page: {s['payments_on_page']} of {s['total_payments']}",
            f"- Payments with incomplete provider evidence: {incomplete}",
            f"- Recovery cases on this page: {s['recovery_cases_on_page']} {esc(s['recovery_cases_by_severity'])}",
            f"- Delivery status: {esc(s['delivery_status'])}; unresolved scope/delivery items: {esc(s['scope_unresolved_items'])}",
            f"- Instruction versions: {s['instruction_versions']}; hash chain intact: {s['instruction_hash_chain_intact']}; payment hold active: {s['payment_hold_active']}",
            f"- Authorization records: {s['authorization_records']} ({s['authorization_records_inconsistent']} inconsistent)",
            f"- All citations resolved: {s['citations_all_resolved']}", f"- {esc(s['no_conclusion_is_drawn'])}", ""]


def _scope(r: dict) -> list[str]:
    s = r["scope_and_delivery"]
    if not s["available"]:
        return ["## Scope and delivery", "", f"_{esc(s['reason'])}_", ""]
    lines = ["## Scope and delivery", "", f"Stage 3 evidence report hash: `{code(s['evidence_report_sha256'])}`", "", "### Agreement versions", ""]
    lines += _list(s["agreement"]["baselines"], lambda b: f"- Version {b['version']} ({esc(b['kind'])}), hash `{code(b['content_sha256'])}`, {len(b['requirements'])} requirements")
    lines += ["", "### Changes", ""] + _list(s["changes"], lambda c: f"- {esc(c['title'])}: **{esc(c['status'])}**, authorization {esc(c['authorization'])}")
    lines += ["", "### Buyer evidence (mailbox-verified; not legal identity)", "", esc(s["buyer_evidence"]["caveat"]),
              f"Delivery status: **{esc(s['buyer_evidence']['delivery_status'])}**", ""]
    lines += _list(s["buyer_evidence"]["decisions"], lambda d: f"- {esc(d['decided_at'])} {esc(d['decision'])} by {esc(d['buyer_email_masked'])}")
    lines += ["", "### Seller claims (unconfirmed)", ""] + _list(s["seller_claims"], lambda c: f"- {esc(c['text'])}")
    ai = s["ai_judgments"]
    lines += ["", "### AI interpretations (never fact)", "", "_No analysis has been run._" if ai is None else esc(ai["label"])]
    if ai:
        lines += [f"- [{esc(f['status'])}/{esc(f['confidence'])}] {esc(f['claim'])}" for f in ai["findings"]]
    lines += ["", "### Unresolved", ""] + _list(s["unresolved"], lambda u: f"- **{esc(u['type'])}**: {esc(u['text'])}")
    return lines + [""]


def _instructions(r: dict) -> list[str]:
    i = r["payment_instructions"]
    h = i["hold"]
    lines = ["## Payment instruction integrity", "", f"Hash chain intact: **{i['hash_chain_intact']}** ({i['version_count']} versions)",
             f"Hold active: **{h['active']}** {esc(', '.join(h['reasons']))}. {esc(h['note'])}", ""]
    lines += _list(i["versions"], lambda v: (
        f"- v{v['version']} `{code(v['fingerprint'])}` ({esc(v['provenance'])}; {esc(v['verification_status'])}; class {esc(v['evidence_class'])}) "
        f"{num(v['amount_minor'])} {esc(v['currency'])} minor units to {esc(v['recipient_email'])}; fingerprint recomputes: {v['fingerprint_recomputes']}"))
    lines += ["", "### Change requests", ""] + _list(i["changes"], lambda c: (
        f"- `{code(c['id'])}` {esc(c['state'])}: {num(c['amount_minor'])} {esc(c['currency'])} to {esc(c['recipient_email'])}; seller's stated reason (claim): {esc(c['reason'])}"))
    return lines + [""]


def _authorizations(r: dict) -> list[str]:
    i = r["payment_instructions"]
    lines = ["## Buyer-side authorization records", "", f"> {esc(i['authorization_limitation'])}", ""]
    lines += _list(i["authorizations"], lambda a: (
        f"- {esc(a['created_at'])} {esc(a['outcome'])} by {esc(a['approver_email_masked'])} via {esc(a['trust_basis'])} - label {esc(a['label'])}; "
        f"real-world identity verified: **{a['real_world_identity_verified']}**; independent of seller: {esc(a['independent_of_seller'])}; "
        f"record consistent: {a['record_consistent']} {esc(', '.join(a['record_problems']))}"))
    lines += ["", "### Enrolled authenticators", ""] + _list(i["authenticators"], lambda a: f"- `{code(a['id'])}` {esc(a['buyer_email_masked'])} {esc(a['status'])} ({esc(a['trust_basis'])})")
    return lines + [""]


def _payments(r: dict) -> list[str]:
    p = r["payments"]
    lines = ["## Payments: seller claims vs provider observations", "", esc(p["authority"]), "",
             f"Page: offset {p['page']['offset']}, limit {p['page']['limit']}, {p['page']['returned']} of {p['page']['total_payments']} payments", ""]
    lines += _list(p["payments"], lambda x: (
        f"- `{code(x['payment_id'])}` {esc(x['invoice_number'])}{' (SIMULATED)' if x['is_simulated'] else ''} [{esc(x.get('provider_provenance', 'UNLABELLED_PROVIDER'))}]: reconciliation status **{esc(x['authoritative_status'])}**; "
        f"provider observed paid {num(x['provider_observations']['paid_minor'])} (complete: {x['provider_observations']['evidence_complete']}, "
        f"gaps: {x['provider_observations']['evidence_gap_count']}); seller claimed {num(x['seller_claims']['claimed_paid_minor'])} "
        f"(unverified); **{esc(x['claim_vs_provider'])}**"))
    return lines + [""]


def _gaps(r: dict) -> list[str]:
    g = r["payments"]["evidence_gaps"]
    return ["## Provider evidence gaps", "", esc(g["warning"]), f"Total {g['total']}, shown {g['shown']}{' (truncated)' if g['truncated'] else ''}", ""] + _list(
        g["items"], lambda x: f"- `{code(x['id'])}` payment `{code(x['payment_id'])}` {esc(x['kind'])}/{esc(x['reason'])}, raw hash `{code(x['raw_sha256'])}`") + [""]


def _cases(r: dict) -> list[str]:
    c = r["payments"]["recovery_cases"]
    lines = ["## Recovery cases (advisory only)", "", f"Cases hash `{code(c['cases_sha256'])}`. These recommend human checks; nothing here acts.", ""]
    for case in c["items"]:
        lines += [f"### {esc(case['severity'])} {esc(case['category'])} (`{code(case['case_id'])}`)", "",
                  f"Confidence: {esc(case['confidence'])}; evidence complete: {case['evidence_complete']}", "", esc(case["explanation"]), "",
                  f"**Recommended human action:** {esc(case['recommended_next_action'])}", "",
                  "Citations: " + (", ".join(f"{esc(x['kind'])}:`{code(x['id'])}`" for x in case["citations"]) or "none"), "",
                  "Never automated: " + "; ".join(esc(a) for a in case["prohibited_automated_actions"]), ""]
    return lines if c["items"] else lines + [_NONE, ""]


def _citations(r: dict) -> list[str]:
    c = r["citation_check"]
    lines = ["## Citation check", "", f"Distinct citations checked: {c['distinct_checked']}; all resolved: **{c['all_resolved']}**", ""]
    if c["problems"]:
        lines += ["**Unresolved citations were found. Do not rely on the sections that cite them.**"]
        lines += [f"- {esc(p['kind'])} `{code(esc(p['id']))}`: {esc(p['reason'])}" for p in c["problems"]]
    return lines + [""]


def _truncation(r: dict) -> list[str]:
    return ["## Truncation", ""] + [f"- {esc(k)}: {esc(v)}" for k, v in r["truncation"].items()] + [""]


def _limitations(r: dict) -> list[str]:
    return ["## Limitations", ""] + [f"- {esc(t)}" for t in r["limitations"]] + [""]
