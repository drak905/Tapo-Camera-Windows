"""Cloud request signing: golden vector from specs/cloud_account_spec.json [spec:cloud.signing]."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tapo_desktop.core.cloud import signing
from tapo_desktop.core.cloud.signing import ACCESS_KEY, SECRET_KEY, content_md5, sign_headers

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "specs" / "cloud_account_spec.json"


def _spec_item(item_id: str) -> dict:
    items = json.loads(SPEC.read_text(encoding="utf-8"))["items"]
    return next(i for i in items if i["id"] == item_id)["value"]


def test_production_keys_match_spec():
    keys = _spec_item("cloud.signing.keys")["production"]
    assert ACCESS_KEY == keys["access_key"]
    assert SECRET_KEY == keys["secret_key"]


def test_golden_vector_byte_exact():
    gv = _spec_item("cloud.signing")["golden_vector"]
    body = gv["body"].encode("utf-8")
    headers = sign_headers(body, gv["path"], nonce=gv["nonce"], access_key=gv["access_key"],
                           secret_key=gv["secret"])
    assert headers == {"Content-MD5": gv["content_md5"], "X-Authorization": gv["x_authorization"]}
    assert content_md5(body) == gv["content_md5"]


def test_content_md5_of_empty_body_hashes_braces():
    gv = _spec_item("cloud.signing")["golden_vector"]
    assert content_md5(b"") == gv["content_md5_of_empty_body_braces"] == "mZFLkyvTelC5g8XnyQrpOw=="
    assert content_md5(b"{}") == "mZFLkyvTelC5g8XnyQrpOw=="


def test_default_nonce_is_fresh_uuid4_and_defaults_to_production_keys():
    a = sign_headers(b"{}", "/api/v2/account/login")
    b = sign_headers(b"{}", "/api/v2/account/login")
    pat = re.compile(r"^Timestamp=9999999999, Nonce=([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}), "
                     rf"AccessKey={ACCESS_KEY}, Signature=[0-9a-f]{{40}}$")
    ma, mb = pat.match(a["X-Authorization"]), pat.match(b["X-Authorization"])
    assert ma and mb, a["X-Authorization"]
    assert ma.group(1) != mb.group(1)


def test_signature_depends_on_path_and_body():
    gv = _spec_item("cloud.signing")["golden_vector"]
    body = gv["body"].encode("utf-8")
    base = sign_headers(body, gv["path"], nonce=gv["nonce"])["X-Authorization"]
    assert sign_headers(body, "/api/v2/account/login", nonce=gv["nonce"])["X-Authorization"] != base
    assert sign_headers(body + b" ", gv["path"], nonce=gv["nonce"])["X-Authorization"] != base


def test_signature_matches_independent_hmac():
    import base64
    import hashlib
    import hmac

    body, path, nonce = b'{"a":1}', "/eu/api/v2/common/getDeviceListByPage", "00000000-0000-4000-8000-000000000000"
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    sig = hmac.new(signing.SECRET_KEY.encode(), f"{md5}\n9999999999\n{nonce}\n{path}".encode(), hashlib.sha1).hexdigest()
    assert sign_headers(body, path, nonce=nonce)["X-Authorization"].endswith(f"Signature={sig}")
