"""Server-side construction of a log-intelligence request for the read-only advisory endpoint.

The caller supplies only bounded plain-text lines. Everything that gives them authority is decided here: the scope (project and payment), the
evidence identifiers, the provenance (always `untrusted_log`) and the model location. `app.log_intelligence` is imported lazily so that starting
the API never loads it (and never loads Stanza, torch or psutil).
"""
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings

SOURCE_ID = "operator-submitted"
LOG_REF_PREFIX = "log:"
_service_lock = threading.Lock()


def environment_for(labels: dict[str, str], payment_id: str | None) -> str:
    """Logs about a Sandbox-sourced payment are labelled sandbox; everything else is simulation. Informational: logs never gain authority."""
    label = labels.get(f"payment:{payment_id}", "") if payment_id else ""
    return "sandbox" if "SANDBOX" in label else "simulation"


def build_log_batch(project_id: str, payment_id: str | None, lines: list[str], *, environment: str, now: datetime | None = None) -> Any:
    from app.diagnosis.models import Evidence
    from app.log_intelligence.models import LogBatch, LogRecord, ScopedEvidence

    scope = payment_id if payment_id is not None else f"project:{project_id}"
    received = now or datetime.now(timezone.utc)
    records, registry = [], []
    for i, text in enumerate(lines):
        ref = f"{LOG_REF_PREFIX}{i}"
        evidence = Evidence(evidence_id=ref, source="untrusted_log", kind="log", entity_id=f"line{i}", payment_id=scope, status="observed", observed_at=received)
        registry.append(ScopedEvidence(project_id=project_id, payment_id=scope, environment=environment, source_id=SOURCE_ID, provenance="untrusted_log", evidence=evidence))
        records.append(LogRecord(log_id=f"line{i}", source_id=SOURCE_ID, project_id=project_id, payment_id=scope, environment=environment, received_at=received,
                                 provenance="untrusted_log", evidence_ref=ref, text=text))
    return LogBatch(project_id=project_id, payment_id=scope, records=tuple(records), evidence=tuple(registry))


def get_service(app: Any, settings: Settings) -> Any:
    """One lazily created service per configured model directory; no model is loaded until a request asks for Stanza."""
    from app.log_intelligence import LogIntelligenceService

    model_dir = settings.stanza_model_dir.strip() or None
    cached = getattr(app.state, "log_intelligence_service", None)
    if cached is not None and cached[0] == model_dir:
        return cached[1]
    with _service_lock:
        cached = getattr(app.state, "log_intelligence_service", None)
        if cached is None or cached[0] != model_dir:
            cached = (model_dir, LogIntelligenceService(Path(model_dir) if model_dir else None))
            app.state.log_intelligence_service = cached
        return cached[1]
