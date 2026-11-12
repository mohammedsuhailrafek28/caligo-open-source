import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import (
    check_origin, get_current_seller, get_current_session, get_db, get_settings_dep, require_csrf,
)
from app.errors import ApiError
from app.models import Seller, SellerSession, utcnow
from app.schemas import LoginRequest, LoginResponse, MeResponse, RegisterRequest, RegistrationAck, SellerView
from app.security import hash_password, hash_token, new_token, normalize_email, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"], dependencies=[Depends(check_origin)])
audit = logging.getLogger("app.audit")
IP_LIMIT_MULTIPLIER = 4  # shared NATs make per-IP limits looser than per-account limits


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", status_code=201, response_model=RegistrationAck)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings_dep)) -> RegistrationAck:
    throttle = request.app.state.register_throttle
    ip_key = f"register:{_client_ip(request)}"
    if throttle.is_blocked(ip_key):  # checked before the expensive Argon2 hash
        raise ApiError(429, "too_many_attempts", "Too many registrations. Try again later.",
                       headers={"Retry-After": str(settings.login_window_seconds)})
    throttle.record_failure(ip_key)  # every attempt counts, successful or not
    email = normalize_email(body.email)
    password_hash = hash_password(body.password.get_secret_value())  # hash always: uniform timing
    if db.scalar(select(Seller.id).where(Seller.email == email)) is None:
        db.add(Seller(email=email, password_hash=password_hash, display_name=body.display_name.strip()))
        try:
            db.commit()
            audit.info("seller_registered request_id=%s", request.state.request_id)
        except IntegrityError:  # concurrent duplicate: same outward behaviour as "already exists"
            db.rollback()
    return RegistrationAck()


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, response: Response,
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
) -> LoginResponse:
    throttle = request.app.state.login_throttle
    email = normalize_email(body.email)
    email_key, ip_key = f"email:{email}", f"ip:{_client_ip(request)}"
    if throttle.is_blocked(email_key) or throttle.is_blocked(ip_key, IP_LIMIT_MULTIPLIER):
        audit.warning("login_throttled request_id=%s", request.state.request_id)
        raise ApiError(429, "too_many_attempts", "Too many failed attempts. Try again later.",
                       headers={"Retry-After": str(settings.login_window_seconds)})

    seller = db.scalar(select(Seller).where(Seller.email == email))
    if not verify_password(seller.password_hash if seller else None, body.password.get_secret_value()):
        throttle.record_failure(email_key)
        throttle.record_failure(ip_key)
        audit.warning("login_failed request_id=%s", request.state.request_id)
        raise ApiError(401, "invalid_credentials", "Invalid email or password.")
    throttle.reset(email_key)

    session_token, csrf_token = new_token(), new_token()  # fresh pair on every login (no fixation)
    now = utcnow()
    db.execute(delete(SellerSession).where(  # keep the table small: drop this seller's dead sessions
        SellerSession.seller_id == seller.id,
        or_(SellerSession.expires_at <= now, SellerSession.revoked_at.is_not(None))))
    expires_at = now + timedelta(minutes=settings.session_ttl_minutes)
    db.add(SellerSession(seller_id=seller.id, token_hash=hash_token(session_token),
                         csrf_hash=hash_token(csrf_token), created_at=now, expires_at=expires_at))
    db.commit()

    cookie = {"max_age": settings.session_ttl_minutes * 60, "secure": settings.cookie_secure,
              "samesite": settings.cookie_samesite, "path": "/"}
    response.set_cookie(settings.session_cookie_name, session_token, httponly=True, **cookie)
    response.set_cookie(settings.csrf_cookie_name, csrf_token, httponly=False, **cookie)  # JS must echo it
    audit.info("login_succeeded seller_id=%s request_id=%s", seller.id, request.state.request_id)
    return LoginResponse(seller=SellerView.model_validate(seller), csrf_token=csrf_token, expires_at=expires_at)


@router.get("/me", response_model=MeResponse)
def me(seller: Seller = Depends(get_current_seller), session: SellerSession = Depends(get_current_session)) -> MeResponse:
    return MeResponse(seller=SellerView.model_validate(seller), expires_at=session.expires_at)


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(
    request: Request, session: SellerSession = Depends(get_current_session),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
) -> Response:
    session.revoked_at = utcnow()
    db.commit()
    audit.info("logout seller_id=%s request_id=%s", session.seller_id, request.state.request_id)
    response = Response(status_code=204)
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, samesite=settings.cookie_samesite)
    return response
