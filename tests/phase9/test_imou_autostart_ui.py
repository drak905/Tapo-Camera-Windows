"""iMou live view starts by itself (UI side): visible tiles stream through the normal view plan like Tapo tiles, hidden
tiles stop, a waiting tile starts on a click through the controller, the headlines, and the window shown / hidden rule.
Fake services, no network."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.models import StreamQuality  # noqa: E402
from tests.phase9.test_imou_ui import (  # noqa: E402,F401
    FakeImouManager, RETRY_HEADLINE, grid_of, imou_cfg, lan_cfg, make_env, make_window,
)

TRYING_HEADLINE = "iMou - trying again"
USED_HEADLINE = "iMou - daily time used"
WAITING_HEADLINE = "iMou - waiting"
CLICK_HEADLINE = "iMou - click to start"


@pytest.fixture(autouse=True)
def _window_flag_reset():
    from tapo_desktop.ui import imou_status

    yield
    imou_status.set_window_shown(True)


def spy_view_remote_now(env):
    env.remote_now = []
    real = env.controller.view_remote_now

    def wrapper(camera_id):
        env.remote_now.append(camera_id)
        return real(camera_id)

    env.controller.view_remote_now = wrapper


# ================================================================================================ the plan
def test_click_to_start_is_off_for_every_camera():
    from tapo_desktop.ui import imou_status

    assert imou_status.click_to_start(imou_cfg("A")) is False and imou_status.click_to_start(lan_cfg(0)) is False


def test_a_waiting_imou_tile_is_clickable_and_a_tapo_or_playing_tile_is_not():
    from tapo_desktop.core.imou import texts
    from tapo_desktop.ui.video_tile import VideoTile

    tile = VideoTile("x", "x")
    for reason in (texts.CLICK_TEXT, texts.retrying("iMou: the stream ended", 30), texts.budget_used(1200),
                   "iMou: the camera is offline (DV1007).", texts.cap_reached(2), "iMou: no first frame"):
        tile.set_stream_state("FAILED", reason)
        assert tile.waiting_for_click(), reason
    for reason in (texts.PLAYING_TEXT, "ffmpeg exited (code 1)", "Live view is not supported for this Kami camera", ""):
        tile.set_stream_state("FAILED", reason)
        assert not tile.waiting_for_click(), reason
    tile.set_stream_state("PLAYING", texts.CLICK_TEXT)
    assert not tile.waiting_for_click()


def test_a_failed_tapo_tile_is_not_started_by_a_click_next_to_an_imou_tile(make_env, qtbot):
    env = make_env([lan_cfg(0), imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    spy_view_remote_now(env)
    env.controller.stream_state.emit("lan0", "FAILED", "ffmpeg exited (code 1)")
    grid._on_tile_clicked("lan0")
    assert env.remote_now == []


def test_a_click_on_a_waiting_imou_tile_asks_the_controller_once(make_env, qtbot):
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    spy_view_remote_now(env)
    env.controller.stream_state.emit("imou:A", "FAILED", "iMou: no first frame for 30.0 s (stalled)")
    grid._on_tile_clicked("imou:A")
    grid._on_tile_clicked("imou:A")  # connecting now: nothing more
    assert env.remote_now == ["imou:A"] and env.called("stop_view") == []


def test_a_click_on_a_playing_imou_tile_asks_for_nothing(make_env, qtbot):
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    spy_view_remote_now(env)
    env.controller.stream_state.emit("imou:A", "PLAYING", "")
    grid._on_tile_clicked("imou:A")
    assert env.remote_now == []


def test_the_playing_a_recording_tile_is_not_clickable(make_env, qtbot):
    from tapo_desktop.core.imou import texts

    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    spy_view_remote_now(env)
    env.controller.stream_state.emit("imou:A", "FAILED", texts.PLAYING_TEXT)
    grid._on_tile_clicked("imou:A")
    assert env.remote_now == []


# ================================================================================================ headlines and texts
def test_the_headlines():
    from tapo_desktop.core.imou import texts
    from tapo_desktop.ui import imou_status
    from tapo_desktop.ui.video_tile import VideoTile

    tile = VideoTile("x", "x")
    cases = {
        texts.CLICK_TEXT: CLICK_HEADLINE,
        texts.retrying("iMou: the stream ended", 30): TRYING_HEADLINE,
        texts.budget_used(1200): USED_HEADLINE,
        "iMou: free quota used. iMou calls stop until tomorrow (UTC). (FL1002)": WAITING_HEADLINE,
        "iMou: too many requests this minute. Try again in a minute. (OP1039)": WAITING_HEADLINE,
        "iMou: check the App ID and App secret. (SN1001)": WAITING_HEADLINE,
        texts.cap_reached(2): WAITING_HEADLINE,
        texts.OFFLINE_IN_LIST: WAITING_HEADLINE,
        texts.NOT_CONNECTED: WAITING_HEADLINE,
        "iMou: no first frame for 30.0 s (stalled)": RETRY_HEADLINE,
        texts.AUTO_LIMIT_TEXT: RETRY_HEADLINE,
    }
    for reason, headline in cases.items():
        tile.set_stream_state("FAILED", reason)
        assert tile.status_text() == headline, reason
    assert imou_status.headline_for("something else") is None and imou_status.headline_for(texts.PLAYING_TEXT) is None


def test_the_texts():
    import re

    from tapo_desktop.core.imou import texts

    assert re.fullmatch(r"iMou: the daily live time for this camera \(20 minutes\) is used\. It starts again after "
                        r"\d\d:\d\d \(00:00 UTC\) when you click the tile, or raise the limit in the iMou connection "
                        r"window\.", texts.budget_used(1200))
    assert texts.cap_reached(2) == ("iMou: 2 live views at once (limit). Hide a tile or raise the limit in the iMou "
                                    "connection window.")
    assert texts.cap_reached(1).startswith("iMou: 1 live view at once (limit).")
    assert texts.retrying("iMou: the stream ended.", 30) == "iMou: the stream ended. Trying again in 30 seconds."
    assert texts.AUTO_LIMIT_TEXT == "iMou: too many automatic restarts today. Click the tile to try again."
    assert texts.run_limit(600).startswith("iMou: playback stopped after 10 minutes")  # the limit stays for playback
    assert not hasattr(texts, "idle_stopped")


def test_the_budget_text_names_the_next_utc_midnight_in_local_time():
    from datetime import datetime, timezone

    from tapo_desktop.core.imou import texts

    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc).timestamp()
    midnight = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc).timestamp()
    expected = datetime.fromtimestamp(midnight).strftime("%H:%M")
    assert f"after {expected} (00:00 UTC)" in texts.budget_used(1200, now)


# ================================================================================================ window hidden or minimized
def test_hiding_the_window_stops_the_imou_streams_only_and_showing_it_starts_them_again(make_env, qtbot):
    env = make_env([lan_cfg(0), imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    window.hide()
    qtbot.waitUntil(lambda: env.called("stop_view") == [("stop_view", "imou:A")], timeout=3000)
    window.show()
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 3, timeout=3000)
    assert env.called("start_view")[-1][1:] == ("imou:A", StreamQuality.SD, 640, 360)
    assert env.called("stop_view") == [("stop_view", "imou:A")]


def test_a_minimized_window_state_stops_the_imou_streams_too(make_env, qtbot):
    from PyQt6.QtCore import QEvent

    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    window.setWindowState(window.windowState() | window.windowState().WindowMinimized)
    window.changeEvent(QEvent(QEvent.Type.WindowStateChange))  # the offscreen platform may not send it by itself
    assert env.called("stop_view") == [("stop_view", "imou:A")]
    window.setWindowState(window.windowState() & ~window.windowState().WindowMinimized)
    window.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)


# ================================================================================================ app start (B1)
def test_the_iMou_connection_is_restored_before_the_window_exists_but_only_when_interactive(tmp_path, monkeypatch, qtbot):
    from PyQt6.QtWidgets import QApplication

    from tapo_desktop import app as app_mod
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.imou_account import ImouAccountManager
    from tapo_desktop.ui import main_window

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(ImouAccountManager, "restore", lambda self: order.append("restore"))
    real_init = main_window.MainWindow.__init__

    def init(self, *a, **kw):
        order.append("window")
        real_init(self, *a, **kw)

    monkeypatch.setattr(main_window.MainWindow, "__init__", init)
    qapp = QApplication.instance()
    for interactive, expected in ((False, ["window"]), (True, ["restore", "window"])):
        order.clear()
        window, controller, _accounts = app_mod._build(qapp, ConfigStore(tmp_path / "config.json"), tmp_path,
                                                       interactive=interactive)
        try:
            assert order == expected
        finally:
            window.deleteLater()
            controller.stop_all()


def test_a_broken_restore_before_the_window_does_not_stop_the_app(tmp_path, monkeypatch, qtbot):
    from PyQt6.QtWidgets import QApplication

    from tapo_desktop import app as app_mod
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.imou_account import ImouAccountManager

    def boom(self):
        raise RuntimeError("vault broken")

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    monkeypatch.setattr(ImouAccountManager, "restore", boom)
    window, controller, _accounts = app_mod._build(QApplication.instance(), ConfigStore(tmp_path / "config.json"),
                                                   tmp_path, interactive=True)
    window.deleteLater()
    controller.stop_all()
