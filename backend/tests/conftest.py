import socket

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.paypal_sandbox import PayPalSandboxProvider
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, SETTINGS_CREDENTIALS, FakeClock, FakePayPal
from tests.helpers import login, register


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Unit tests must never reach PayPal (or anything else): any real socket connect fails the test."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _check(address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):  # loopback is the event loop's own socketpair
            raise AssertionError(f"real network access attempted during a unit test: {host!r}")

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Run every test from an empty directory so a developer's local .env is never read,
    and never let a cached Settings object leak between tests."""
    monkeypatch.chdir(tmp_path)
    for name in ("APP_ENV", "DATABASE_URL", "DEV_AUTH_ENABLED", "PAYPAL_CLIENT_SECRET", "AI_API_KEY",
                 "PAYPAL_MODE", "COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def make_app(tmp_path):
    """Build an isolated app (own SQLite file, own mock provider, own throttle)."""
    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        values = {"database_url": f"sqlite:///{tmp_path / f'api{counter['n']}.sqlite3'}", "paypal_mode": "mock"}
        values.update(overrides)
        return create_app(Settings(**values))

    return _make


@pytest.fixture()
def app(make_app):
    return make_app()


@pytest.fixture()
def seller_factory(app):
    """seller_factory('a@example.com') -> logged-in Session with its own cookie jar."""

    def _make(email: str = "seller@example.com"):
        client = TestClient(app)
        assert register(client, email).status_code == 201
        return login(client, email)

    return _make


@pytest.fixture()
def seller(seller_factory):
    return seller_factory("seller@example.com")


# --- sandbox adapter wired to a fake PayPal (no network) -------------------------------------

BUYER = "buyer@example.org"


@pytest.fixture()
def fake():
    return FakePayPal()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def sleeps():
    return []


@pytest.fixture()
def sandbox_provider(fake, clock, sleeps):
    return PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, merchant_email=MERCHANT, transport=fake.transport(),
                                 clock=clock, sleep=sleeps.append)


@pytest.fixture()
def sandbox_app(tmp_path, sandbox_provider):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'sandbox.sqlite3'}", paypal_mode="sandbox", **SETTINGS_CREDENTIALS,
        paypal_sandbox_merchant_email=MERCHANT,
        paypal_sandbox_recipient_allowlist=BUYER)
    return create_app(settings, payment_provider=sandbox_provider)


@pytest.fixture()
def sandbox_seller_factory(sandbox_app):
    def _make(email: str = "seller@example.com"):
        client = TestClient(sandbox_app)
        assert register(client, email).status_code == 201
        return login(client, email)
    return _make


@pytest.fixture()
def sb(sandbox_seller_factory):
    return sandbox_seller_factory()
