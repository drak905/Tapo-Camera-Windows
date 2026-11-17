"""Shared pytest fixtures (owned by lane-fixture; contract: docs/CONTRACT_FIXTURE.md).

Fixtures: ``mock_camera`` (secure), ``legacy_mock_camera``, ``dual_lens_camera``, ``chaos_proxy``
(factory), ``ffmpeg_path``, ``ffprobe_path``, ``tmp_data_dir``, ``loopback_http_stream_ok`` /
``require_loopback_http_stream`` (detect / skip when a local HTTP filter such as AdGuard hijacks
loopback HTTP streams, which breaks the :8800 media protocol over real TCP).
Tests marked ``hardware`` are skipped unless env ``TAPO_HW_IP`` is set.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.fixtures.mock_tapo_server import ChaosProxy, MockTapoCamera


def pytest_collection_modifyitems(config, items):
    if os.environ.get("TAPO_HW_IP"):
        return
    skip = pytest.mark.skip(reason="hardware test: set TAPO_HW_IP (and TAPO_HW_* credentials) to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


def _which(name: str, env: str | None = None) -> str | None:
    if env and os.environ.get(env) and Path(os.environ[env]).exists():
        return os.environ[env]
    return shutil.which(name)


@pytest.fixture
def mock_camera():
    """Secure (encrypt_type 3) mock camera, started; stopped at teardown."""
    cam = MockTapoCamera(secure=True)
    cam.start()
    try:
        yield cam
    finally:
        cam.stop()


@pytest.fixture
def legacy_mock_camera():
    """Legacy (MD5 login, plaintext JSON) mock camera."""
    cam = MockTapoCamera(secure=False, model="C200")
    cam.start()
    try:
        yield cam
    finally:
        cam.stop()


@pytest.fixture
def dual_lens_camera():
    """Secure dual-lens camera: RTSP also serves /stream6 and /stream7."""
    cam = MockTapoCamera(secure=True, model="C240D", dual_lens=True)
    cam.start()
    try:
        yield cam
    finally:
        cam.stop()


@pytest.fixture
def chaos_proxy():
    """Factory: ``chaos_proxy(host, port) -> started ChaosProxy``; all stopped at teardown."""
    proxies: list[ChaosProxy] = []

    def make(host: str, port: int, listen_port: int = 0) -> ChaosProxy:
        p = ChaosProxy(host, port, listen_port=listen_port)
        p.start()
        proxies.append(p)
        return p

    try:
        yield make
    finally:
        for p in proxies:
            p.stop()


@pytest.fixture
def ffmpeg_path() -> str:
    path = _which("ffmpeg", "TAPO_FFMPEG")
    if not path:
        pytest.skip("ffmpeg not found on PATH (or TAPO_FFMPEG)")
    return path


@pytest.fixture
def ffprobe_path() -> str:
    path = _which("ffprobe", "TAPO_FFPROBE")
    if not path:
        pytest.skip("ffprobe not found on PATH (or TAPO_FFPROBE)")
    return path


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch) -> Path:
    """Isolated app data dir; sets env TAPO_DESKTOP_HOME to it."""
    d = tmp_path / "tapo_desktop_home"
    d.mkdir()
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(d))
    return d


@pytest.fixture(scope="session")
def loopback_http_stream_ok() -> bool:
    """False when a local HTTP filter (e.g. AdGuard for Windows) hijacks loopback HTTP streams.

    The :8800 media protocol keeps sending multipart bytes after the HTTP 200; such filters
    answer them with their own ``400 Bad request`` page, so real-TCP :8800 tests cannot pass.
    """
    from tests.fixtures.raw_clients import loopback_http_stream_passthrough

    return loopback_http_stream_passthrough()


@pytest.fixture
def require_loopback_http_stream(loopback_http_stream_ok) -> None:
    """Skip the test when loopback HTTP streams are hijacked (see ``loopback_http_stream_ok``)."""
    if not loopback_http_stream_ok:
        pytest.skip("a local HTTP filter (e.g. AdGuard) rewrites loopback HTTP streams; exclude python.exe "
                    "from its filtering to run real-TCP :8800 media tests")
