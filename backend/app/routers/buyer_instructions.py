"""Public buyer-side verification of payment-instruction changes (R2). No seller session or cookie is read here: possession of an
operator-established authenticator is the credential. There is deliberately NO enrolment, replacement or reset route."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import schemas
from app.deps import get_db, get_payment_provider
from app.errors import ApiError
from app.payment_provider import PaymentProvider
from app.services import authenticators as auth
from app.services import instruction_verification as service

router = APIRouter(prefix="/api/v1/buyer/instruction-changes", tags=["buyer-instruction-verification"])


def limit_verification_ops(request: Request) -> None:
    """Per-client budget (in-memory, per process). The per-challenge attempt budget and the per-authenticator challenge limit are durable."""
    throttle = request.app.state.buyer_throttle
    key = "instruction-verification:" + (request.client.host if request.client else "unknown")
    if throttle.is_blocked(key):
        raise ApiError(429, "too_many_attempts", "Too many requests. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(key)


def _keyring(request: Request) -> auth.Keyring | None:
    settings = request.app.state.settings
    try:
        return auth.parse_keyring(settings.authenticator_keys.get_secret_value(), settings.authenticator_active_key_id)
    except auth.KeyringError:
        return None  # an unusable keyring fails closed exactly like a missing one


@router.post("/challenge", response_model=schemas.InstructionChallengeResponse, dependencies=[Depends(limit_verification_ops)])
def challenge(body: schemas.InstructionChallengeRequest, db: Session = Depends(get_db)) -> schemas.InstructionChallengeResponse:
    return service.challenge(db, body.project_id, body.change_id, body.buyer_email)


@router.post("/decide", response_model=schemas.InstructionDecideResponse, dependencies=[Depends(limit_verification_ops)])
def decide(body: schemas.InstructionDecideRequest, request: Request, db: Session = Depends(get_db),
           provider: PaymentProvider = Depends(get_payment_provider)) -> schemas.InstructionDecideResponse:
    return service.decide(db, provider, _keyring(request), body)
