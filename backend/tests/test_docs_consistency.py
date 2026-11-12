"""R0: documentation regression tests. Docs are part of the contract; these fail when they drift from the code."""
import re
from pathlib import Path

from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
ARCH = ROOT / "docs" / "ARCHITECTURE.md"
SCAFFOLDING_MODELS = ("Deliverable", "DeliveryEvent", "EvidenceClaim")
SCAFFOLDING_TABLES = ("deliverables", "delivery_events", "evidence_claims")


def read(path: Path) -> str:
    return path.read_text(encoding="utf8")


def test_every_database_table_is_inventoried_in_the_architecture_doc():
    text = read(ARCH)
    missing = [name for name in Base.metadata.tables if f"`{name}`" not in text]
    assert missing == [], f"tables missing from docs/ARCHITECTURE.md: {missing}"


def test_unused_scaffolding_is_documented_and_really_unused():
    """If a route or service starts using these models the docs must be updated first (this test then fails on purpose)."""
    used_in = []
    for folder in ("routers", "services"):
        for path in (BACKEND / "app" / folder).glob("*.py"):
            source = read(path)
            used_in += [f"{path.name}:{model}" for model in SCAFFOLDING_MODELS if re.search(rf"\b{model}\b", source)]
    assert used_in == [], f"scaffolding now referenced by {used_in}; update docs/ARCHITECTURE.md and this test"
    section = read(ARCH).split("## Unused scaffolding", 1)[1].split("\n## ", 1)[0]
    assert all(f"`{table}`" in section for table in SCAFFOLDING_TABLES)
    assert "DeliveryEventType" in section and "EvidenceStatus" in section


def test_dependency_points_for_the_planned_work_point_at_real_code():
    """Every `file.py::symbol` named in the R1-R6 dependency section must exist, so the plan cannot silently rot."""
    section = read(ARCH).split("## Dependency points for R1-R6", 1)[1].split("\n## ", 1)[0]
    refs = re.findall(r"`((?:app|tests)/[\w/]+\.py)::(\w+)`", section)
    assert len(refs) >= 15, "dependency section looks empty"
    for file, symbol in refs:
        path = BACKEND / file
        assert path.exists(), f"{file} does not exist"
        assert symbol in read(path), f"{symbol} not found in {file}"


def test_status_documents_do_not_carry_the_pre_evidence_baseline():
    for name in ("README.md",):
        text = read(ROOT / name)
        assert "371" not in text, f"{name} still cites the old 371-test baseline"
        assert "no remote exists" not in text.lower() and "Stage 2C implemented" not in text, f"{name} is stale"


def test_the_api_contract_header_matches_the_implemented_stage():
    header = read(ROOT / "contracts" / "API_CONTRACT.md").splitlines()[0]
    assert "Stage 3" in header and "Stage 2C implemented" not in header


def test_the_api_contract_documents_the_derived_delivery_status():
    text = read(ROOT / "contracts" / "API_CONTRACT.md")
    assert "`DeliveryStatus`" in text and "delivery_status" in text
    assert "reserved" in text.lower() and "DELIVERED" in text and "DISPUTED" in text

