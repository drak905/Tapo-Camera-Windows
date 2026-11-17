"""Tier 1: ffmpeg discovery and command line  (docs/CONTRACT_MEDIA.md `media_engine.ffmpeg`)."""
from __future__ import annotations

import logging
import os
import sys

import pytest

from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args, find_ffmpeg

pytestmark = pytest.mark.unit

URL = "rtsp://camuser:Sup3rSecretPw@192.168.1.10:554/stream2"


def _pair(args: list[str], flag: str) -> str:
    """Value following the first occurrence of ``flag``."""
    assert flag in args, f"{flag} missing from {args}"
    i = args.index(flag)
    assert i + 1 < len(args), f"{flag} has no value"
    return args[i + 1]


def _all_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def test_args_exclude_executable_and_are_strings():
    args = build_ffmpeg_args(URL, 640, 360)
    assert isinstance(args, list) and args
    assert all(isinstance(a, str) for a in args)
    assert not os.path.basename(args[0]).lower().startswith("ffmpeg")


def test_input_options_low_latency_tcp_and_hwaccel_before_input():
    args = build_ffmpeg_args(URL, 640, 360)
    i_input = args.index("-i")
    assert args[i_input + 1] == URL
    assert _pair(args, "-rtsp_transport") == "tcp"
    assert _pair(args, "-fflags") == "nobuffer"
    assert _pair(args, "-flags") == "low_delay"
    assert _pair(args, "-hwaccel") == "d3d11va"
    for flag in ("-rtsp_transport", "-fflags", "-flags", "-hwaccel"):
        assert args.index(flag) < i_input, f"{flag} must be an input option"


def test_hwaccel_none_omits_flag():
    args = build_ffmpeg_args(URL, 640, 360, hwaccel=None)
    assert "-hwaccel" not in args
    assert "d3d11va" not in args


def test_custom_hwaccel_value_is_passed():
    assert _pair(build_ffmpeg_args(URL, 640, 360, hwaccel="dxva2"), "-hwaccel") == "dxva2"


def test_transport_udp():
    assert _pair(build_ffmpeg_args(URL, 640, 360, transport="udp"), "-rtsp_transport") == "udp"


@pytest.mark.parametrize(("w", "h"), [(640, 360), (1280, 720), (320, 180), (1920, 1080)])
def test_video_output_is_scaled_padded_rgb24_rawvideo_on_stdout(w, h):
    args = build_ffmpeg_args(URL, w, h)
    i_input = args.index("-i")
    assert _pair(args, "-map") == "0:v:0"
    vf = _pair(args, "-vf")
    assert vf.startswith(f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    assert _pair(args, "-pix_fmt") == "rgb24"
    assert "rawvideo" in _all_values(args, "-f")
    assert "pipe:1" in args
    i_pipe = args.index("pipe:1")
    for flag in ("-map", "-vf", "-pix_fmt"):
        assert i_input < args.index(flag) < i_pipe
    # "-f rawvideo" must directly govern pipe:1 (the last -f before pipe:1)
    f_before_pipe = [args[i + 1] for i in range(i_input, i_pipe) if args[i] == "-f"]
    assert f_before_pipe and f_before_pipe[-1] == "rawvideo"


def test_no_audio_output_without_audio_url():
    args = build_ffmpeg_args(URL, 640, 360)
    assert "s16le" not in args
    assert "0:a:0?" not in args
    assert "-ar" not in args
    assert "-ac" not in args
    assert args[-1] == "pipe:1"


def test_audio_output_with_audio_url():
    audio = "tcp://127.0.0.1:50123"
    args = build_ffmpeg_args(URL, 640, 360, audio_url=audio)
    assert args[-1] == audio
    i_pipe = args.index("pipe:1")
    tail = args[i_pipe + 1:]
    assert tail[tail.index("-map") + 1] == "0:a:0?"
    assert tail[tail.index("-ac") + 1] == "1"
    assert tail[tail.index("-ar") + 1] == "8000"
    assert tail[tail.index("-f") + 1] == "s16le"
    assert _all_values(args, "-map") == ["0:v:0", "0:a:0?"]


def test_fps_limit_only_when_requested():
    plain = build_ffmpeg_args(URL, 640, 360)
    assert "-r" not in plain
    assert "fps=" not in _pair(plain, "-vf")
    limited = build_ffmpeg_args(URL, 640, 360, fps=10)
    assert _all_values(limited, "-r") == ["10"] or "fps=10" in _pair(limited, "-vf")


def test_password_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    build_ffmpeg_args(URL, 640, 360, audio_url="tcp://127.0.0.1:1")
    build_ffmpeg_args(URL, 1280, 720, hwaccel=None)
    assert "Sup3rSecretPw" not in caplog.text


# ---------------------------------------------------------------------------- find_ffmpeg


@pytest.fixture
def isolated_search(monkeypatch, tmp_path):
    """No TAPO_FFMPEG, empty PATH, fake sys.executable dir, no _MEIPASS."""
    monkeypatch.delenv("TAPO_FFMPEG", raising=False)
    empty = tmp_path / "empty_path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "python.exe"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    return tmp_path


def _touch_exe(directory):
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "ffmpeg.exe"
    p.write_bytes(b"MZ")
    return p


def test_find_ffmpeg_honours_env(isolated_search, monkeypatch):
    custom = _touch_exe(isolated_search / "custom")
    _touch_exe(isolated_search / "app")  # also beside sys.executable: env must win
    monkeypatch.setenv("TAPO_FFMPEG", str(custom))
    assert os.path.normcase(find_ffmpeg()) == os.path.normcase(str(custom))


def test_find_ffmpeg_beside_executable(isolated_search):
    exe = _touch_exe(isolated_search / "app")
    _touch_exe(isolated_search / "empty_path")
    assert os.path.normcase(find_ffmpeg()) == os.path.normcase(str(exe))


def test_find_ffmpeg_in_meipass(isolated_search, monkeypatch):
    bundle = _touch_exe(isolated_search / "meipass")
    monkeypatch.setattr(sys, "_MEIPASS", str(isolated_search / "meipass"), raising=False)
    assert os.path.normcase(find_ffmpeg()) == os.path.normcase(str(bundle))


def test_find_ffmpeg_on_path(isolated_search):
    on_path = _touch_exe(isolated_search / "empty_path")
    assert os.path.normcase(os.path.abspath(find_ffmpeg())) == os.path.normcase(str(on_path))


def test_find_ffmpeg_missing_raises(isolated_search):
    with pytest.raises(FileNotFoundError):
        find_ffmpeg()
