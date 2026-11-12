"""Deterministic Markdown rendering of the evidence report. All user-supplied text is escaped (no raw HTML, no table breakouts)."""
import html
import unicodedata
from typing import Any


def esc(value: Any) -> str:
    raw = str(value if value is not None else "")
    raw = "".join(" " if c.isspace() else c for c in raw if unicodedata.category(c) not in ("Cc", "Cf") or c.isspace())
    text = " ".join(raw.split())
    text = html.escape(text, quote=False)
    for char in ("\\", "`", "|", "*", "_", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def _bullets(items: list[dict], empty: str, line) -> list[str]:
    return [line(i) for i in items] if items else [f"_{empty}_"]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Evidence report: {esc(report['project']['title'])}", "",
             f"Project: `{report['project']['id']}` · report version {report['report_version']}",
             f"Report SHA-256: `{report['report_sha256']}`", "", f"> {esc(report['disclaimer'])}", ""]
    lines += _agreement(report) + _changes(report) + _mapping(report) + _timeline(report)
    lines += _observations(report) + _claims(report) + _ai(report) + _buyer(report) + _payments(report)
    lines += _unresolved(report) + _limitations(report)
    return "\n".join(lines) + "\n"


def _agreement(report: dict) -> list[str]:
    lines = ["## Agreement", ""]
    for baseline in report["agreement"]["baselines"]:
        lines.append(f"### Version {baseline['version']} ({baseline['kind']}) - hash `{baseline['content_sha256']}`")
        lines += [f"- **{esc(r['key'])}** {esc(r['title'])}: {esc(r['description'])}" for r in baseline["requirements"]]
        lines.append("")
    return lines


def _changes(report: dict) -> list[str]:
    return ["## Changes", ""] + _bullets(report["changes"], "No changes were proposed.", lambda c: (
        f"- {esc(c['title'])} ({', '.join(map(esc, c['requirement_keys']))}) - status **{esc(c['status'])}**, "
        f"authorization **{esc(c['authorization'])}**")) + [""]


def _mapping(report: dict) -> list[str]:
    def line(m: dict) -> str:
        if not m["artifacts"]:
            return f"- **{esc(m['key'])}** {esc(m['title'])}: no linked artifact"
        found = "; ".join(f"{esc(a['name'])} v{a['version']} ({a['content_verification']}, sha256 {a['sha256'] or 'none'}, link seller-asserted)"
                          for a in m["artifacts"])
        return f"- **{esc(m['key'])}** {esc(m['title'])}: {found}"

    return ["## Requirement mapping", ""] + _bullets(report["requirement_mapping"], "None.", line) + [""]


def _timeline(report: dict) -> list[str]:
    return ["## Timeline", "", "Ordered by the server's recording time.", ""] + _bullets(
        report["timeline"], "No events.", lambda t: f"- {esc(t['at'])} [{esc(t['actor_role'])}] {esc(t['summary'])}") + [""]


def _observations(report: dict) -> list[str]:
    return ["## Confirmed observations", "", "Recorded by the server or by a verified buyer action.", ""] + _bullets(
        report["confirmed_observations"], "None.",
        lambda o: f"- {o['at']} {esc(o['text'])} ({', '.join(o['refs'])})") + [""]


def _claims(report: dict) -> list[str]:
    return ["## Seller claims", "", "Asserted by the seller; nothing independent confirms these.", ""] + _bullets(
        report["seller_claims"], "None.", lambda c: f"- {esc(c['text'])} ({', '.join(c['refs'])})") + [""]


def _ai(report: dict) -> list[str]:
    ai = report["ai_judgments"]
    if ai is None:
        return ["## AI judgments", "", "_No analysis has been run._", ""]
    stale = "" if ai["matches_current_evidence"] else " **Evidence has changed since this analysis.**"
    lines = ["## AI judgments", "", f"{esc(ai['label'])}{stale}", f"Input hash: `{ai['input_sha256']}`", ""]
    lines += [f"- [{f['status']}/{f['confidence']}] {esc(f['claim'])} - {esc(f['explanation'])} ({', '.join(f['source_refs'])})"
              for f in ai["findings"]]
    return lines + [""]


def _buyer(report: dict) -> list[str]:
    evidence = report["buyer_evidence"]
    lines = ["## Buyer evidence", "", f"{esc(evidence['caveat'])}", f"Delivery status: **{evidence['delivery_status']}**", ""]
    lines += _bullets(evidence["decisions"], "No verified buyer decisions.", lambda d: (
        f"- {esc(d['decided_at'])} {esc(d['decision'])} by {esc(d['buyer_email_masked'])} ({esc(d['verification_method'])})"
        + (f" - \"{esc(d['statement'])}\"" if d["statement"] else "")))
    return lines + [""]


def _payments(report: dict) -> list[str]:
    payments = report["payments"]
    lines = ["## Payments", "", esc(payments["note"]), ""]
    lines += _bullets(payments["records"], "No invoice exists.", lambda p: (
        f"- {esc(p['provider'])}{' (SIMULATED)' if p['is_simulated'] else ''} [{esc(p.get('provider_provenance', 'UNLABELLED_PROVIDER'))}]: {int(p['amount_minor'])} {esc(p['currency'])} minor units, "
        f"status {esc(p['provider_status'])}, paid {int(p['amount_paid_minor'])}, refunded {int(p['amount_refunded_minor'])}"))
    return lines + [""]


def _unresolved(report: dict) -> list[str]:
    return ["## Unresolved", ""] + _bullets(report["unresolved"], "Nothing unresolved was detected.",
                                           lambda u: f"- **{u['type']}**: {esc(u['text'])} ({', '.join(u['refs'])})") + [""]


def _limitations(report: dict) -> list[str]:
    return ["## Limitations", ""] + [f"- {esc(text)}" for text in report["limitations"]] + [""]
