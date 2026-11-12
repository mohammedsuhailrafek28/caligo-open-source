"""Shared test helpers (importable; conftest.py holds only fixtures)."""
from fastapi.testclient import TestClient

PASSWORD = "correct horse battery staple"  # test-only credential, not a secret


class Session:
    """A TestClient (own cookie jar) plus the CSRF token for the logged-in seller."""

    def __init__(self, client: TestClient):
        self.client = client
        self.csrf: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def post(self, url, json=None, headers=None, **kw):
        return self.client.post(url, json=json, headers={**self.headers, **(headers or {})}, **kw)

    def get(self, url, **kw):
        return self.client.get(url, **kw)


def register(client: TestClient, email: str, password: str = PASSWORD, name: str = "Demo Seller"):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password, "display_name": name})


def login(client: TestClient, email: str, password: str = PASSWORD) -> Session:
    s = Session(client)
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    s.csrf = response.json()["csrf_token"]
    return s
