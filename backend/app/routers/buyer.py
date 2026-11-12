"""Public buyer endpoints (Stage 3C). No seller session or cookie is read here: the buyer proves control of a mailbox."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import schemas
from app.deps import get_db
from app.errors import ApiError
from app.mailer import Mailer
from app.services import buyer as buyer_service

router = APIRouter(prefix="/api/v1/buyer", tags=["buyer"])


def get_mailer(request: Request) -> Mailer:
    mailer = request.app.state.mailer
    if mailer is None:
        raise ApiError(503, "buyer_mail_disabled", "Buyer e-mail delivery is disabled.")
    return mailer


def limit_buyer_ops(request: Request, _mailer: Mailer = Depends(get_mailer)) -> None:
    """Per-client budget so codes and sessions cannot be ground through."""
    throttle = request.app.state.buyer_throttle
    key = request.client.host if request.client else "unknown"
    if throttle.is_blocked(key):
        raise ApiError(429, "too_many_attempts", "Too many requests. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(key)


@router.post("/challenge", status_code=202, response_model=schemas.BuyerChallengeAck, dependencies=[Depends(limit_buyer_ops)])
def challenge(body: schemas.BuyerChallengeRequest, db: Session = Depends(get_db),
              mailer: Mailer = Depends(get_mailer)) -> schemas.BuyerChallengeAck:
    """Mails a one-time code. The answer is identical whether or not the link is valid."""
    buyer_service.send_challenge(db, mailer, body.token)
    return schemas.BuyerChallengeAck()


@router.post("/verify", response_model=schemas.BuyerSessionResponse, dependencies=[Depends(limit_buyer_ops)])
def verify(body: schemas.BuyerVerifyRequest, db: Session = Depends(get_db)) -> schemas.BuyerSessionResponse:
    return buyer_service.verify_code(db, body.token, body.code)


@router.post("/decide", response_model=schemas.BuyerDecisionResponse, dependencies=[Depends(limit_buyer_ops)])
def decide(body: schemas.BuyerDecideRequest, db: Session = Depends(get_db)) -> schemas.BuyerDecisionResponse:
    return buyer_service.decide(db, body)
