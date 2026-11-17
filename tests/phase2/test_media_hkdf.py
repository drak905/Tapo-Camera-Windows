""":8800 HKDF stream encryption [spec:stream.local8800.encryption.hkdf], as used by Tapo C200 fw 1.5.7
(Key-Exchange: cipher=AES_128_CBC algorithm=HKDF salt=<hex>; per part X-Nonce = IV, X-Data-Hmac =
base64(HMAC-SHA256(hmac_key, ciphertext)))."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tapo_desktop.core.vod import session as S


def _ref_hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=info).derive(ikm)


def _ref_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(p.update(data) + p.finalize()) + enc.finalize()


KEX = {"cipher": "AES_128_CBC", "username": "admin", "padding": "PKCS7_16", "algorithm": "HKDF",
       "encrypt_type": "3", "nonce": "0123456789abcdef0123456789abcdef", "salt": "fedcba9876543210fedcba9876543210"}
SECRET = S.transform_password("local-access-token", "3")


@pytest.mark.unit
def test_hkdf_keys_match_reference_derivation():
    keys = S.derive_stream_hkdf_keys(KEX, SECRET)
    ikm = f"{KEX['nonce']}:{SECRET}".encode()
    assert keys == (_ref_hkdf(ikm, KEX["salt"].encode(), b"stream_hkdf_aes_key"),
                    _ref_hkdf(ikm, KEX["salt"].encode(), b"stream_hkdf_hmac_key"))


@pytest.mark.unit
def test_hkdf_keys_none_for_md5_mode_or_unknown_user():
    assert S.derive_stream_hkdf_keys({"username": "admin", "nonce": "ab"}, SECRET) is None
    assert S.derive_stream_hkdf_keys({**KEX, "username": "someone"}, SECRET) is None


@pytest.mark.unit
def test_decrypt_hkdf_part_verifies_hmac_and_uses_x_nonce_iv():
    aes_key, mac_key = S.derive_stream_hkdf_keys(KEX, SECRET)
    data = os.urandom(1000)
    iv = os.urandom(16)
    ct = _ref_encrypt(aes_key, iv, data)
    mac = base64.b64encode(hmac.new(mac_key, ct, hashlib.sha256).digest()).decode()
    assert S.decrypt_hkdf_part(ct, aes_key, mac_key, iv.hex(), mac) == data
    assert S.decrypt_hkdf_part(ct, aes_key, mac_key, iv.hex(), None) == data  # HMAC header optional
    with pytest.raises(S.TapoProtocolError):
        S.decrypt_hkdf_part(ct, aes_key, mac_key, iv.hex(), base64.b64encode(b"x" * 32).decode())
    with pytest.raises(S.TapoProtocolError):
        S.decrypt_hkdf_part(ct, aes_key, mac_key, "zz", mac)


@pytest.mark.integration
def test_live_view_with_hkdf_camera(mock_camera, ffmpeg_path):
    from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
    from tapo_desktop.core.media_engine.supervisor import StreamState

    mock_camera.media_kex_mode = "hkdf"
    frames: list = []
    playing = threading.Event()
    sup = LocalStreamSupervisor(mock_camera.host, "cloudpass", 640, 360, resolution="VGA",
                                port=mock_camera.media_port, on_frame=frames.append,
                                on_state=lambda s, r: s is StreamState.PLAYING and playing.set())
    sup.start()
    try:
        assert playing.wait(15)
        time.sleep(3)
        assert len(frames) > 20
        assert len(frames[-1].data) == 640 * 360 * 3
        assert sup.stats()["reconnects"] == 0
    finally:
        sup.stop()


@pytest.mark.integration
def test_hkdf_tampered_part_is_dropped_not_fatal(mock_camera, ffmpeg_path):
    from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
    from tapo_desktop.core.media_engine.supervisor import StreamState

    mock_camera.media_kex_mode = "hkdf"
    mock_camera.media_corrupt_hmac_every = 10  # every 10th part carries a wrong HMAC
    frames: list = []
    playing = threading.Event()
    sup = LocalStreamSupervisor(mock_camera.host, "cloudpass", 640, 360, resolution="VGA",
                                port=mock_camera.media_port, on_frame=frames.append,
                                on_state=lambda s, r: s is StreamState.PLAYING and playing.set())
    sup.start()
    try:
        assert playing.wait(15)
        time.sleep(3)
        assert sup.state is StreamState.PLAYING
        assert len(frames) > 10
    finally:
        sup.stop()


@pytest.mark.integration
def test_download_with_hkdf_camera(mock_camera, ffmpeg_path, tmp_path):
    import subprocess

    from tapo_desktop.core.vod.downloader import RecordingDownloader

    mock_camera.media_kex_mode = "hkdf"
    clip = mock_camera.state.recordings[0]
    out = RecordingDownloader(mock_camera.host, "cloudpass", port=mock_camera.media_port).download(
        clip, tmp_path / "clip.mp4")
    probe = subprocess.run([str(ffmpeg_path).replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                            "stream=codec_name", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert "h264" in probe.stdout
