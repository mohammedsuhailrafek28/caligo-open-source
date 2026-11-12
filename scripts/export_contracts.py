"""Regenerate contracts/*.schema.json from the Pydantic models (the source of truth).

Run from the repo root:  backend/.venv/Scripts/python scripts/export_contracts.py
`backend/tests/test_contracts.py` fails if committed files drift from the models.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import schemas  # noqa: E402

_NAMES = [
    "DeliveryEventView", "InvoiceRequest", "LoginRequest", "LoginResponse", "MeResponse",
    "PaymentEventView", "PaymentLink", "ProjectDetail", "ProjectDraft", "ProjectView", "RegisterRequest",
    "RegistrationAck", "ScopeAnalysis", "ScopeRecordCreate", "ScopeRecordView", "SellerView",
    "ReviewResolutionRequest", "SendInvoiceRequest",
    "BaselineCreate", "BaselineView", "ScopeChangeCreate", "ScopeChangeView", "AuditEventView",
    "ArtifactCreate", "ArtifactLinkCreate", "ArtifactView", "TimelineItem",
    "BuyerRequestCreate", "BuyerRequestView", "BuyerChallengeRequest", "BuyerChallengeAck", "BuyerVerifyRequest",
    "BuyerSessionResponse", "BuyerDecideRequest", "BuyerDecisionView", "BuyerDecisionResponse",
    "AnalysisFinding", "AnalysisRunView",
    "PaymentInstructionCreate", "PaymentInstructionView", "InstructionChangeCreate", "InstructionChangeWithdraw",
    "InstructionChangeView", "HoldStatus", "PaymentInstructionState",
    "BuyerAuthenticatorView", "InstructionChangeVerificationView", "InstructionChallengeRequest", "InstructionValues",
    "InstructionChangeForBuyer", "InstructionChallengeResponse", "InstructionDecideRequest", "InstructionDecideResponse",
    "PaymentObservationView", "SellerObservationCreate", "SellerObservationResult", "MerchantLedgerImportRequest",
    "MerchantLedgerImportView", "ObservationCitation", "ObservationFinding", "PaymentComparison", "ObservationComparison",
    "CaseCitation", "RecoveryCase", "RecoveryCaseList",
    "RecoveryPlanCreate", "RecoveryPlanApprove", "RecoveryPlanView", "RecoveryPlanList", "RecoveryPreview", "RecoveryExecutionResult",
    "RecoveryEventList", "DivergenceList", "DiagnosisResponse", "IntegrationHealthResponse",
    "LogIntelligenceRequest", "LogIntelligenceResponse",
    "PayeeCreate", "PayeeDestinationChange", "PayeeVersionView", "PayeeView", "PayeeScreenRequest", "PayeeScreenResponse",
]
CONTRACT_MODELS = {name: getattr(schemas, name) for name in _NAMES}


def render(model) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def render_openapi() -> str:
    """The FastAPI OpenAPI document, built from a throwaway in-memory-config app (no .env, no network, no existing database)."""
    import tempfile

    from app.config import Settings
    from app.main import create_app

    class IsolatedSettings(Settings):  # init values only: neither .env nor the shell environment can change the generated document
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
            return (init_settings,)

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(IsolatedSettings(database_url=f"sqlite:///{Path(tmp) / 'openapi.sqlite3'}", paypal_mode="mock",
                                  buyer_mail_mode="fake", ai_mode="demo"))
        try:
            return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
        finally:
            app.state.engine.dispose()


if __name__ == "__main__":
    out_dir = ROOT / "contracts"
    (out_dir / "openapi.json").write_text(render_openapi(), encoding="utf8", newline="\n")
    print("wrote contracts/openapi.json")
    for name, model in CONTRACT_MODELS.items():
        (out_dir / f"{name}.schema.json").write_text(render(model), encoding="utf8", newline="\n")
        print(f"wrote contracts/{name}.schema.json")
