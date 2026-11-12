import hashlib
import io
import json
from pathlib import Path

import pytest

from app.log_intelligence import setup_models

MANIFEST = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "log-intelligence" / "model-manifest.json").read_text(encoding="utf-8"))


def _fake_fetch(data):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return lambda url, timeout: Response(data)


def _expected(data, source=None):
    entry = dict(MANIFEST["files"]["resources.json"], sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    if source:
        entry["source"] = source
    return {"files": {"resources.json": entry}}


def test_the_manifest_pins_resources_json_to_an_immutable_commit():
    assert setup_models.IMMUTABLE_SOURCE.match(MANIFEST["files"]["resources.json"]["source"])
    assert "/main/" not in MANIFEST["files"]["resources.json"]["source"]


def test_pinned_resources_replace_whatever_was_downloaded(tmp_path):
    data = b'{"pinned": true}'
    (tmp_path / "resources.json").write_bytes(b'{"moved": "main"}')
    setup_models.pin_resources(tmp_path, _expected(data, MANIFEST["files"]["resources.json"]["source"]), fetch=_fake_fetch(data))
    assert (tmp_path / "resources.json").read_bytes() == data


def test_pinned_resources_with_a_wrong_hash_are_refused_and_not_written(tmp_path):
    expected = _expected(b"good", MANIFEST["files"]["resources.json"]["source"])
    with pytest.raises(RuntimeError):
        setup_models.pin_resources(tmp_path, expected, fetch=_fake_fetch(b"evil"))
    assert not (tmp_path / "resources.json").exists()


def test_a_mutable_source_is_refused_before_any_fetch(tmp_path):
    expected = _expected(b"x", "https://raw.githubusercontent.com/stanfordnlp/stanza-resources/main/resources_1.15.0.json")

    def forbidden(url, timeout):
        raise AssertionError("must not fetch a mutable source")

    with pytest.raises(RuntimeError):
        setup_models.pin_resources(tmp_path, expected, fetch=forbidden)
