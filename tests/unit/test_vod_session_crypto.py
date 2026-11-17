"""Tier 1: pure :8800 helpers - digest auth, Key-Exchange, AES key/iv, client part framing.

[spec:stream.local8800.auth.digest, stream.local8800.encryption.md5, stream.local8800.framing,
vod.stream.flow_control_ack]
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.errors import TapoError, TapoProtocolError
from tapo_desktop.core.vod.session import (
    CLIENT_BOUNDARY,
    DEFAULT_MEDIA_PASSWORD,
    DEFAULT_MEDIA_USERNAME,
    DEVICE_BOUNDARY,
    build_ack_part,
    decrypt_part,
    derive_stream_key_iv,
    digest_authorization,
    digest_response,
    encode_client_part,
    generate_cnonce,
    parse_key_exchange,
    parse_www_authenticate,
    transform_password,
)
from tests.unit._media_helpers import aes_encrypt, parse_auth_params, ref_digest_response, ref_key_iv

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ WWW-Authenticate
def test_parse_www_authenticate_full():
    ch = parse_www_authenticate(
        'Digest realm="TP-LINK IP-CAM", qop="auth,auth-int", nonce="a1b2", opaque="o9", '
        "algorithm=SHA-256, encrypt_type=3, userhash=true"
    )
    assert ch == {
        "realm": "TP-LINK IP-CAM",
        "qop": "auth,auth-int",
        "nonce": "a1b2",
        "opaque": "o9",
        "algorithm": "SHA-256",
        "encrypt_type": "3",
        "userhash": "true",
    }


def test_parse_www_authenticate_lowercases_keys_and_tolerates_spacing():
    ch = parse_www_authenticate('digest Realm="r",NONCE="n" ,  qop=auth')
    assert ch["realm"] == "r"
    assert ch["nonce"] == "n"
    assert ch["qop"] == "auth"


@pytest.mark.parametrize("value", ['Basic realm="x"', "", "Digest", 'Digest realm="r", qop="auth"'])
def test_parse_www_authenticate_rejects_non_digest_or_missing_nonce(value):
    with pytest.raises(TapoProtocolError):
        parse_www_authenticate(value)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200))
def test_parse_www_authenticate_never_raises_foreign_errors(value):
    try:
        parse_www_authenticate(value)
    except TapoError:
        pass


# ------------------------------------------------------------------ password transform / cnonce
def test_transform_password_md5_default():
    expected = hashlib.md5(b"cloudpass").hexdigest().upper()
    assert transform_password("cloudpass") == expected
    assert transform_password("cloudpass", None) == expected
    assert transform_password("cloudpass", "2") == expected


@pytest.mark.parametrize("et", [3, "3"])
def test_transform_password_sha256_for_encrypt_type_3(et):
    assert transform_password("cloudpass", et) == hashlib.sha256(b"cloudpass").hexdigest().upper()


def test_transform_password_utf8():
    assert transform_password("pässwörd") == hashlib.md5("pässwörd".encode()).hexdigest().upper()


def test_generate_cnonce_format_and_randomness():
    values = {generate_cnonce() for _ in range(20)}
    assert len(values) > 15
    for v in values:
        assert re.fullmatch(r"(?:[a-z][0-9]){8}", v), v


# ------------------------------------------------------------------ digest response (RFC 7616 vectors)
RFC = dict(
    username="Mufasa",
    realm="http-auth@example.org",
    password="Circle of Life",
    method="GET",
    uri="/dir/index.html",
    nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
    nc="00000001",
    cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
    qop="auth",
)


def test_digest_response_rfc7616_md5():
    assert digest_response(**RFC, algorithm="MD5") == "8ca523f5e9506fed4657c9700eebdbec"


def test_digest_response_rfc7616_sha256():
    assert (
        digest_response(**RFC, algorithm="SHA-256")
        == "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"
    )


@pytest.mark.parametrize("alg", ["MD5-sess", "SHA-256-sess"])
def test_digest_response_sess_variants(alg):
    expected = ref_digest_response(**{k: RFC[k] for k in RFC}, algorithm=alg)
    assert digest_response(**RFC, algorithm=alg) == expected
    assert expected != ref_digest_response(**RFC, algorithm=alg.replace("-sess", ""))


# ------------------------------------------------------------------ Authorization header
CH_MD5 = {"realm": "TP-LINK IP-CAM", "qop": "auth", "nonce": "8c5d1f22", "opaque": "6494321", "algorithm": "MD5"}


def test_digest_authorization_exact_md5():
    pw_t = hashlib.md5(b"cloudpass").hexdigest().upper()
    resp = ref_digest_response("admin", "TP-LINK IP-CAM", pw_t, "POST", "/stream", "8c5d1f22", "00000001",
                               "a1b2c3d4e5f6g7h8", "auth", "MD5")
    header = digest_authorization(CH_MD5, "cloudpass", cnonce="a1b2c3d4e5f6g7h8")
    assert header == (
        'Digest username="admin",realm="TP-LINK IP-CAM",uri="/stream",algorithm=MD5,nonce="8c5d1f22",'
        f'nc=00000001,cnonce="a1b2c3d4e5f6g7h8",qop=auth,response="{resp}",opaque="6494321"'
    )


def test_digest_authorization_sha256_encrypt_type_3():
    ch = dict(CH_MD5, algorithm="SHA-256", encrypt_type="3")
    pw_t = hashlib.sha256(b"cloudpass").hexdigest().upper()
    resp = ref_digest_response("admin", ch["realm"], pw_t, "POST", "/stream", ch["nonce"], "00000001",
                               "k1k2k3k4k5k6k7k8", "auth", "SHA-256")
    header = digest_authorization(ch, "cloudpass", cnonce="k1k2k3k4k5k6k7k8")
    p = parse_auth_params(header)
    assert p["algorithm"] == "SHA-256"
    assert p["response"] == resp
    assert len(resp) == 64


def test_digest_authorization_first_qop_token_and_default_algorithm():
    ch = {"realm": "r", "qop": "auth,auth-int", "nonce": "n1", "opaque": "o"}
    header = digest_authorization(ch, "pw", cnonce="c0c0c0c0c0c0c0c0")
    p = parse_auth_params(header)
    assert p["qop"] == "auth"
    assert p["algorithm"] == "MD5"
    assert p["nc"] == "00000001"
    assert p["uri"] == "/stream"
    assert p["response"] == ref_digest_response(
        "admin", "r", hashlib.md5(b"pw").hexdigest().upper(), "POST", "/stream", "n1", "00000001",
        "c0c0c0c0c0c0c0c0", "auth", "MD5",
    )


def test_digest_authorization_empty_password_uses_default():
    a = digest_authorization(CH_MD5, "", cnonce="a1a1a1a1a1a1a1a1")
    b = digest_authorization(CH_MD5, DEFAULT_MEDIA_PASSWORD, cnonce="a1a1a1a1a1a1a1a1")
    assert a == b


def test_digest_authorization_userhash():
    ch = dict(CH_MD5, userhash="true")
    header = digest_authorization(ch, "cloudpass", cnonce="a1b2c3d4e5f6g7h8")
    p = parse_auth_params(header)
    hashed_user = hashlib.md5(b"admin:TP-LINK IP-CAM").hexdigest()
    assert p["username"] == hashed_user
    assert header.endswith(",userhash=true")
    pw_t = hashlib.md5(b"cloudpass").hexdigest().upper()
    assert p["response"] == ref_digest_response(hashed_user, "TP-LINK IP-CAM", pw_t, "POST", "/stream",
                                                "8c5d1f22", "00000001", "a1b2c3d4e5f6g7h8", "auth", "MD5")


def test_digest_authorization_generates_cnonce_when_omitted():
    p = parse_auth_params(digest_authorization(CH_MD5, "cloudpass"))
    assert re.fullmatch(r"(?:[a-z][0-9]){8}", p["cnonce"])


def test_digest_authorization_never_contains_password():
    header = digest_authorization(CH_MD5, "Sup3rSecretCloud", cnonce="a1b2c3d4e5f6g7h8")
    assert "Sup3rSecretCloud" not in header
    assert hashlib.md5(b"Sup3rSecretCloud").hexdigest().upper() not in header


# ------------------------------------------------------------------ Key-Exchange
def test_parse_key_exchange():
    ke = parse_key_exchange('username="admin" nonce="0d4a5e9f3b2c1a77" cipher="AES_128_CBC" padding="PKCS7_16"')
    assert ke == {"username": "admin", "nonce": "0d4a5e9f3b2c1a77", "cipher": "AES_128_CBC", "padding": "PKCS7_16"}


def test_parse_key_exchange_unquoted_and_extra_spaces():
    ke = parse_key_exchange('  username=none   nonce=ABC123 algorithm="HKDF" salt="s1" ')
    assert ke["username"] == "none"
    assert ke["nonce"] == "ABC123"
    assert ke["algorithm"] == "HKDF"
    assert ke["salt"] == "s1"


@pytest.mark.parametrize("value", ['username="admin"', "", "garbage"])
def test_parse_key_exchange_requires_nonce(value):
    with pytest.raises(TapoProtocolError):
        parse_key_exchange(value)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200))
def test_parse_key_exchange_never_raises_foreign_errors(value):
    try:
        parse_key_exchange(value)
    except TapoError:
        pass


def test_derive_key_iv_admin_uses_transformed_password():
    pw_t = hashlib.md5(b"cloudpass").hexdigest().upper()
    key, iv = derive_stream_key_iv({"username": "admin", "nonce": "0d4a5e9f3b2c1a77"}, pw_t)
    assert key == hashlib.md5(f"0d4a5e9f3b2c1a77:{pw_t}".encode()).digest()
    assert iv == hashlib.md5(b"admin:0d4a5e9f3b2c1a77").digest()
    assert (key, iv) == ref_key_iv("admin", "0d4a5e9f3b2c1a77", pw_t)
    assert len(key) == len(iv) == 16


def test_derive_key_iv_none_uses_default_password():
    key, iv = derive_stream_key_iv({"username": "none", "nonce": "ff00"}, "IGNORED")
    assert key == hashlib.md5(b"ff00:TPL075526460603").digest()
    assert iv == hashlib.md5(b"none:ff00").digest()


def test_derive_key_iv_unknown_username_is_none():
    assert derive_stream_key_iv({"username": "someone", "nonce": "ff00"}, "X") is None


# ------------------------------------------------------------------ AES per part
def test_decrypt_part_round_trip_and_fresh_iv_per_part():
    key, iv = ref_key_iv("admin", "n", "P")
    a = b"\x47" + bytes(range(187)) * 3
    b = b"second part"
    ca, cb = aes_encrypt(a, key, iv), aes_encrypt(b, key, iv)
    # every part decrypts independently with the same fixed IV
    assert decrypt_part(cb, key, iv) == b
    assert decrypt_part(ca, key, iv) == a
    assert decrypt_part(ca, key, iv) == a


@settings(max_examples=100, deadline=None)
@given(st.binary(max_size=4096))
def test_decrypt_part_property(data):
    key, iv = ref_key_iv("admin", "nonce", "PW")
    assert decrypt_part(aes_encrypt(data, key, iv), key, iv) == data


@pytest.mark.parametrize("body", [b"", b"\x00" * 15, b"\x01" * 17])
def test_decrypt_part_bad_length(body):
    key, iv = ref_key_iv("admin", "n", "P")
    with pytest.raises(TapoProtocolError):
        decrypt_part(body, key, iv)


def test_decrypt_part_wrong_key_bad_padding():
    key, iv = ref_key_iv("admin", "n", "P")
    other, _ = ref_key_iv("admin", "n", "Q")
    ct = aes_encrypt(b"x" * 40, key, iv)
    try:
        out = decrypt_part(ct, other, iv)
    except TapoProtocolError:
        return
    assert out != b"x" * 40  # padding happened to look valid; data must still differ


# ------------------------------------------------------------------ client framing
def test_encode_client_part_json_exact():
    assert (CLIENT_BOUNDARY, DEVICE_BOUNDARY) == ("--client-stream-boundary--", "--device-stream-boundary--")
    assert (DEFAULT_MEDIA_USERNAME, DEFAULT_MEDIA_PASSWORD) == ("admin", "TPL075526460603")
    body = b'{"type":"request","seq":1}'
    assert encode_client_part(body) == (
        b"----client-stream-boundary--\r\nContent-Type: application/json\r\n"
        b"Content-Length: 26\r\n\r\n" + body + b"\r\n"
    )


def test_encode_client_part_extra_headers_first_in_order():
    out = encode_client_part(b"ab", content_type="audio/mp2t", headers={"X-If-Encrypt": "1", "X-Session-Id": "9"})
    assert out == (
        b"----client-stream-boundary--\r\nX-If-Encrypt: 1\r\nX-Session-Id: 9\r\n"
        b"Content-Type: audio/mp2t\r\nContent-Length: 2\r\n\r\nab\r\n"
    )


def test_build_ack_part_exact():
    body = json.dumps({"type": "notification", "params": {"event_type": "stream_sequence"}}, separators=(",", ":"))
    assert build_ack_part(25, "9") == (
        b"----client-stream-boundary--\r\nX-Data-Received: 25\r\nX-Session-Id: 9\r\n"
        b"Content-Type: application/json\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body.encode() + b"\r\n"
    )
