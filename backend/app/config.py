from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./local_dev.sqlite3"
    # "mock" is an in-process fake: no network, no credentials. "sandbox" talks to PayPal's TEST environment
    # only. "live"/production is deliberately unrepresentable until separately reviewed and approved.
    paypal_mode: Literal["disabled", "mock", "sandbox"] = "disabled"
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")  # SecretStr: never leaks via repr/logs
    paypal_sandbox_merchant_email: str = ""  # sandbox business account; provider evidence must match it
    # Comma-separated sandbox buyer identities that invoices may be addressed to. Nothing else can be invoiced.
    paypal_sandbox_recipient_allowlist: str = ""
    ai_mode: Literal["disabled", "demo"] = "disabled"  # "demo" = deterministic non-production analysis; no external AI
    # Buyer approval e-mails. "fake" keeps messages in an in-process outbox (tests/demo only); there is no real mailer yet.
    buyer_mail_mode: Literal["disabled", "fake"] = "disabled"
    # LOCAL DEVELOPMENT ONLY. Empty (default) = off. A path makes the FAKE mailer also append each message (link token, one-time code) as one JSON
    # line to that local file, so a frontend developer can read the buyer's "inbox" like a mail catcher. Needs BUYER_MAIL_MODE=fake and a
    # non-production APP_ENV; the file holds live one-time secrets, so keep it out of Git (dev_outbox/ is ignored) and delete it after use.
    dev_outbox_file: str = ""
    buyer_portal_base_url: str = "http://localhost:3000"
    buyer_ops_per_minute: int = Field(default=30, ge=1, le=10_000)  # per client address on the public buyer endpoints
    buyer_mail_per_minute: int = Field(default=10, ge=1, le=1000)  # per seller: buyer e-mails requested
    ai_api_key: SecretStr = SecretStr("")
    cors_origins: str = "http://localhost:3000"  # comma-separated; also the CSRF Origin allow-list
    dev_auth_enabled: bool = False  # reserved; unused (real auth is implemented)

    # Seller sessions
    session_cookie_name: str = "pem_session"
    csrf_cookie_name: str = "pem_csrf"
    session_ttl_minutes: int = Field(default=480, ge=5, le=60 * 24 * 14)
    cookie_secure: bool = False  # False only for plain-HTTP local development
    cookie_samesite: Literal["lax", "strict"] = "lax"
    login_max_failures: int = Field(default=5, ge=1, le=100)  # per account, per window
    login_window_seconds: int = Field(default=900, ge=30, le=86_400)
    register_max_per_window: int = Field(default=10, ge=1, le=1000)  # registrations per IP per window
    # R2: encryption keyring for authenticator secrets, "id:base64key,id2:base64key2" (32-byte keys); never logged. Empty = disabled.
    authenticator_keys: SecretStr = SecretStr("")
    authenticator_active_key_id: str = ""
    payment_instruction_required: bool = False  # True: no invoice without a recorded instruction (default keeps the older flow)
    payment_ops_per_minute: int = Field(default=30, ge=1, le=10_000)  # per seller: create/send/sync
    # OPTIONAL local Stanza model cache for the advisory log-intelligence endpoint. Empty (default) = deterministic only; a "stanza" request then
    # reports effective_mode=deterministic with fallback_reason=model_not_configured. A server path only: never taken from a request.
    stanza_model_dir: str = ""
    max_body_bytes: int = Field(default=1_000_000, ge=1_000, le=50_000_000)  # declared Content-Length cap

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sandbox_recipient_list(self) -> list[str]:
        return [e.strip().lower() for e in self.paypal_sandbox_recipient_allowlist.split(",") if e.strip()]

    @model_validator(mode="after")
    def enforce_safe_combinations(self) -> "Settings":
        if self.app_env == "production":
            if self.dev_auth_enabled:
                raise ValueError("DEV_AUTH_ENABLED must be false when APP_ENV=production")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true when APP_ENV=production")
            if self.paypal_mode != "disabled":
                raise ValueError("PAYPAL_MODE must be disabled in production until a real adapter is approved")
            if self.buyer_mail_mode != "disabled":
                raise ValueError("BUYER_MAIL_MODE must be disabled in production until a real mailer is approved")
            if self.dev_outbox_file:
                raise ValueError("DEV_OUTBOX_FILE must be empty in production")
            if self.ai_mode != "disabled":
                raise ValueError("AI_MODE must be disabled in production until a real, reviewed provider exists")
        if self.paypal_mode == "sandbox":
            if not self.paypal_client_id or not self.paypal_client_secret.get_secret_value():
                raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required when PAYPAL_MODE=sandbox")
            if not self.paypal_sandbox_merchant_email.strip():
                raise ValueError("PAYPAL_SANDBOX_MERCHANT_EMAIL is required when PAYPAL_MODE=sandbox")
            if not self.sandbox_recipient_list:
                raise ValueError("PAYPAL_SANDBOX_RECIPIENT_ALLOWLIST is required when PAYPAL_MODE=sandbox")
        if self.dev_outbox_file and self.buyer_mail_mode != "fake":
            raise ValueError("DEV_OUTBOX_FILE needs BUYER_MAIL_MODE=fake")
        if "*" in self.cors_origin_list:
            raise ValueError("CORS_ORIGINS must list explicit origins; '*' is not allowed with cookies")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
