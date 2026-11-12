"""The dev-only outbox file: off by default, never in production, never reachable over HTTP, and never in a seller-facing response."""
import json
import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from tests.helpers import login, register
from tests.test_buyer_approvals import BUYER, CODE_RE, TOKEN_RE, challenge, request_approval
from tests.test_scope_baselines import freeze, make_project, propose


def _seller_and_change(app):
    client = TestClient(app)
    assert register(client, "seller@example.com").status_code == 201
    seller = login(client, "seller@example.com")
    pid = make_project(seller)
    freeze(seller, pid)
    return seller, pid, propose(seller, pid).json()


def test_the_outbox_file_is_off_by_default():
    assert Settings(_env_file=None).dev_outbox_file == ""


def test_the_outbox_file_is_rejected_in_production_and_without_fake_mail(tmp_path):
    target = str(tmp_path / "out.jsonl")
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=True, buyer_mail_mode="fake", dev_outbox_file=target)
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=True, buyer_mail_mode="disabled", dev_outbox_file=target)
    with pytest.raises(ValidationError):
        Settings(buyer_mail_mode="disabled", dev_outbox_file=target)


def test_without_the_setting_no_file_is_written(make_app, tmp_path):
    app = make_app(buyer_mail_mode="fake")
    seller, pid, change = _seller_and_change(app)
    assert request_approval(seller, pid, change["id"]).status_code == 201
    assert list(tmp_path.glob("**/*.jsonl")) == []


def test_messages_are_appended_as_json_lines_and_match_the_in_memory_outbox(make_app, tmp_path):
    target = tmp_path / "dev_outbox" / "buyer.jsonl"
    app = make_app(buyer_mail_mode="fake", dev_outbox_file=str(target))
    seller, pid, change = _seller_and_change(app)
    assert request_approval(seller, pid, change["id"]).status_code == 201
    token = TOKEN_RE.search(app.state.mailer.outbox[-1].body).group(1)
    assert challenge(TestClient(app), token).status_code == 202
    lines = [json.loads(line) for line in target.read_text(encoding="utf8").splitlines()]
    assert len(lines) == len(app.state.mailer.outbox) == 2
    assert [line["to"] for line in lines] == [BUYER, BUYER]
    assert TOKEN_RE.search(lines[0]["body"]).group(1) == token
    assert CODE_RE.search(lines[1]["body"])


def test_seller_facing_responses_never_carry_the_token_or_the_code(make_app, tmp_path):
    target = tmp_path / "buyer.jsonl"
    app = make_app(buyer_mail_mode="fake", dev_outbox_file=str(target))
    seller, pid, change = _seller_and_change(app)
    created = request_approval(seller, pid, change["id"])
    token = TOKEN_RE.search(app.state.mailer.outbox[-1].body).group(1)
    assert challenge(TestClient(app), token).status_code == 202
    code = CODE_RE.search(app.state.mailer.outbox[-1].body).group(1)
    seen = [created.text]
    for path in ("buyer-requests", "audit-events", "scope-changes", "unified-report"):
        response = seller.get(f"/api/v1/projects/{pid}/{path}")
        seen.append(response.text)
    blob = "\n".join(seen)
    assert token not in blob and not re.search(rf"\b{code}\b", blob)


def test_no_http_route_serves_the_outbox(make_app, tmp_path):
    app = make_app(buyer_mail_mode="fake", dev_outbox_file=str(tmp_path / "buyer.jsonl"))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any("outbox" in path for path in paths)
