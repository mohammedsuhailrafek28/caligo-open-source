"""PayPal Evidence MVP API (Stage 2C: seller auth, projects, scope records, MOCK or SANDBOX invoicing)."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import make_engine
from app.errors import install_error_handlers, install_log_redaction, new_request_id
from app.payment_provider import MockPayPalProvider, PaymentProvider
from app.services.analysis import DemoAnalysisProvider
from app.mailer import FakeOutboxMailer
from app.routers import auth, buyer, buyer_instructions, evidence, instructions, observations, payees, projects, recovery, state_recovery, unified_report
from app.security import LoginThrottle


def build_provider(settings: Settings) -> PaymentProvider | None:
    if settings.paypal_mode == "mock":
        return MockPayPalProvider()
    if settings.paypal_mode == "sandbox":
        from app.paypal_sandbox import PayPalSandboxProvider  # imported lazily: no network client unless configured
        return PayPalSandboxProvider(
            settings.paypal_client_id, settings.paypal_client_secret,
            merchant_email=settings.paypal_sandbox_merchant_email or None)
    return None


def create_app(settings: Settings | None = None, payment_provider: PaymentProvider | None = None) -> FastAPI:
    """`payment_provider` lets tests inject an adapter wired to a fake transport."""
    settings = settings or get_settings()
    production = settings.app_env == "production"
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Shutdown: release the provider's HTTP connection pool and the database pool; never let one failure skip the other.
        for name, action in (("payment provider", getattr(app.state.payment_provider, "close", None)),
                             ("database engine", app.state.engine.dispose)):
            if action is None:
                continue
            try:
                action()
            except Exception as exc:  # shutdown must complete
                logging.getLogger("app.audit").error("shutdown_close_failed target=%s error_type=%s", name, type(exc).__name__)

    app = FastAPI(
        lifespan=lifespan, title="PayPal Evidence MVP API", version="0.3.0",
        docs_url=None if production else "/docs",
        redoc_url=None, openapi_url=None if production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)  # connects lazily; tables created on first request
    app.state.db_ready = False
    app.state.login_throttle = LoginThrottle(settings.login_max_failures, settings.login_window_seconds)
    app.state.register_throttle = LoginThrottle(settings.register_max_per_window, settings.login_window_seconds)
    app.state.payment_throttle = LoginThrottle(settings.payment_ops_per_minute, 60)
    app.state.observation_read_throttle = LoginThrottle(settings.payment_ops_per_minute * 3, 60)  # reads of observations/comparison
    app.state.payment_provider = payment_provider or build_provider(settings)
    app.state.mailer = FakeOutboxMailer(settings.dev_outbox_file or None) if settings.buyer_mail_mode == "fake" else None  # no real mailer exists
    app.state.analysis_provider = DemoAnalysisProvider() if settings.ai_mode == "demo" else None  # no real AI provider exists
    app.state.analysis_throttle = LoginThrottle(settings.payment_ops_per_minute, 60)
    app.state.buyer_throttle = LoginThrottle(settings.buyer_ops_per_minute, 60)
    app.state.buyer_mail_throttle = LoginThrottle(settings.buyer_mail_per_minute, 60)

    install_log_redaction()
    install_error_handlers(app)

    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = new_request_id()
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_body_bytes:  # chunked bodies: enforce at the proxy
            response = JSONResponse({"error": {"code": "payload_too_large", "message": "Request body too large.",
                                               "request_id": request.state.request_id}}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health", tags=["foundation"])
    def health() -> dict[str, str]:
        return {"status": "ok", "paypal_mode": settings.paypal_mode, "ai_mode": settings.ai_mode}

    @app.get("/api/v1/capabilities", tags=["foundation"])
    def capabilities() -> dict[str, bool | str]:
        return {
            "foundation": True,
            "seller_auth": True,
            "projects": True,
            "scope_records": True,
            "mock_paypal_invoicing": settings.paypal_mode == "mock",
            "paypal_sandbox_invoicing": settings.paypal_mode == "sandbox",
            "paypal_invoicing": False,  # live PayPal: not implemented
            "paypal_transaction_verification": False,  # live PayPal: not implemented
            "delivery_event_capture": settings.buyer_mail_mode != "disabled",
            "buyer_email_verification": settings.buyer_mail_mode != "disabled",
            "ai_scope_analysis": settings.ai_mode != "disabled",
            "dispute_report": True,
            "note": "Payments run against an in-process MOCK or the PayPal SANDBOX (test money) only; live PayPal is not implemented.",
        }

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(instructions.router)
    app.include_router(observations.router)
    app.include_router(recovery.router)
    app.include_router(state_recovery.router)
    app.include_router(payees.router)
    app.include_router(unified_report.router)
    app.include_router(evidence.router)
    app.include_router(buyer.router)
    app.include_router(buyer_instructions.router)
    return app


app = create_app()
