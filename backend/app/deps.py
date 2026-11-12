"""Request dependencies: DB session, session auth, CSRF, Origin check, project ownership."""
import threading
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SchemaOutOfDateError, init_db
from app.errors import ApiError
from app.models import Project, Seller, SellerSession, utcnow
from app.payment_provider import PaymentProvider
from app.security import hash_token, tokens_match

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_init_lock = threading.Lock()


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    app = request.app
    if not app.state.db_ready:
        with _init_lock:
            if not app.state.db_ready:
                try:
                    init_db(app.state.engine)
                except SchemaOutOfDateError:
                    raise ApiError(503, "schema_out_of_date", "The database schema is out of date. Run the migrations (alembic upgrade head).") from None
                app.state.db_ready = True
    with Session(app.state.engine, expire_on_commit=False) as session:
        yield session


def check_origin(request: Request, settings: Settings = Depends(get_settings_dep)) -> None:
    """Reject cross-site browser requests: a present Origin header must be on the allow-list."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in settings.cors_origin_list:
        raise ApiError(403, "origin_not_allowed", "Cross-origin request rejected.")


def get_current_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> SellerSession:
    raw = request.cookies.get(settings.session_cookie_name)
    unauthenticated = ApiError(401, "not_authenticated", "Authentication required.")
    if not raw:
        raise unauthenticated
    row = db.scalar(select(SellerSession).where(SellerSession.token_hash == hash_token(raw)))
    if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
        raise unauthenticated
    return row


def get_current_seller(
    session: SellerSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> Seller:
    seller = db.get(Seller, session.seller_id)
    if seller is None:  # session outlived its seller: treat as logged out
        raise ApiError(401, "not_authenticated", "Authentication required.")
    return seller


def require_csrf(
    request: Request,
    session: SellerSession = Depends(get_current_session),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Double-submit token that is also bound to the server-side session (hash compared)."""
    if request.method in SAFE_METHODS:
        return
    header = request.headers.get("x-csrf-token")
    cookie = request.cookies.get(settings.csrf_cookie_name)
    if not header or not cookie or not tokens_match(header, cookie) or not tokens_match(hash_token(header), session.csrf_hash):
        raise ApiError(403, "csrf_failed", "CSRF token missing or invalid.")


def get_owned_project(
    project_id: str,
    seller: Seller = Depends(get_current_seller),
    db: Session = Depends(get_db),
) -> Project:
    """The only way a route loads a project. Another seller's project is indistinguishable from none."""
    project = db.scalar(select(Project).where(Project.id == project_id, Project.seller_id == seller.id))
    if project is None:
        raise ApiError(404, "not_found", "Project not found.")
    return project


def get_payment_provider(request: Request) -> PaymentProvider:
    provider = request.app.state.payment_provider
    if provider is None:
        raise ApiError(503, "payments_disabled", "Payment integration is disabled.")
    return provider


def limit_observation_reads(request: Request, seller: Seller = Depends(get_current_seller)) -> None:
    """Separate, larger per-seller budget for the (database-heavy) observation and comparison reads, so they cannot starve writes."""
    throttle = request.app.state.observation_read_throttle
    if throttle.is_blocked(seller.id):
        raise ApiError(429, "too_many_attempts", "Too many requests. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(seller.id)


def limit_payment_ops(request: Request, seller: Seller = Depends(get_current_seller)) -> None:
    """Per-seller budget for provider-touching routes: one seller cannot exhaust the shared PayPal app quota or threads."""
    throttle = request.app.state.payment_throttle
    if throttle.is_blocked(seller.id):
        raise ApiError(429, "too_many_attempts", "Too many payment operations. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(seller.id)
