"""iMou camera service (route A, watch only): live view that starts by itself for a visible tile.

* ``start_stream`` is called by the grid for every visible iMou tile (the normal view plan) and by a click on a waiting
  tile (``preempt=True``, the controller's "the user clicked" signal). It takes a live slot from the iMou runtime's own
  cap, checks the daily minutes of this camera, asks the iMou cloud for a temporary RTSP address
  (``createVideoDownloadStreamUrl``; HLS ``bindDeviceLive`` only if that call is refused) and hands it to an
  :class:`~tapo_desktop.core.imou.stream.ImouStreamSupervisor`. SD for grid tiles, HD only in full screen.
* Every start that is not a click is *automatic* and costs vendor quota, so it is guarded (one failure memory per camera,
  module-wide, so a rebuilt service object cannot ask again):

  - a refusal (quota, flow, guard, auth, offline, cap, budget, a vendor refusal, not connected) is never retried: the
    text stays until a click;
  - a lost stream, a network failure, "busy" or "no answer" of an automatic start is retried by itself,
    ``AUTO_RETRY_DELAY_S`` apart, at most ``AUTO_RETRY_MAX`` times; a click is never retried by itself;
  - the retry count restarts only after the stream played ``STABLE_PLAY_S`` without a break;
  - a clean end of a stream that played that long (the address probably expired) restarts once after
    ``AUTO_QUICK_DELAY_S``; it counts in the day's cap of automatic starts but not in the retries;
  - at most ``AUTO_STARTS_PER_DAY`` automatic starts per camera per UTC day; then a text that waits for a click.

* No timer ends a live stream except the daily minutes (the run limit is for playback). The day's minutes end it with a
  text that waits for a click; nothing wakes the tile at 00:00 UTC.
* A cloud HLS address is deleted when the stream stops, fails or the service closes.
* Every other action raises "Not supported for iMou cameras yet"; the capability flags tell the UI to hide them.
* The live URL is a secret: it is never logged and never shown (texts go through ``errors.clean``).
The runtime (client, guard, limits, meter, online map) is set by the iMou account manager once the user has connected.
"""
from __future__ import annotations

import logging
import pathlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from tapo_desktop.core.errors import TapoError
from tapo_desktop.core.imou import notes, texts
from tapo_desktop.core.imou.clock import clock_for, pc_day_bounds
from tapo_desktop.core.imou.errors import (
    PREFIX, RTSP_REFUSED_CODES, ImouAuthError, ImouBlockedError, ImouBudgetError, ImouConnectionError, ImouError,
    ImouLiveExistsError, ImouOfflineError, ImouProtocolError, ImouRateError, ImouVendorError, clean,
)
from tapo_desktop.core.imou.guard import next_utc_midnight
from tapo_desktop.core.imou.records import build_runs, two_clocks
from tapo_desktop.core.imou.runtime import ImouRuntime
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import (
    CameraConfig, DayNightMode, DeviceInfo, MotionEvent, Preset, RecordingClip, StreamQuality,
)
from tapo_desktop.services.camera_service import CameraService
from tapo_desktop.services.imou_playback import ImouPlayback
from tapo_desktop.services.stream_policy import decoder_for

log = logging.getLogger(__name__)

IMOU_UNSUPPORTED = texts.UNSUPPORTED
_HD = (StreamQuality.HD, StreamQuality.TELE_HD)

#: Automatic retry of a lost stream or a network failure: never sooner than this many seconds, at most this many times.
AUTO_RETRY_DELAY_S = 30.0
AUTO_RETRY_MAX = 3
#: A stream that played this long without a break starts a new retry streak; a clean end after it restarts quickly.
STABLE_PLAY_S = 60.0
AUTO_QUICK_DELAY_S = 2.0
#: Automatic starts of one camera per UTC day (clicks are not counted).
AUTO_STARTS_PER_DAY = 12
#: Vendor codes that mean "busy" or "no answer": worth a delayed retry, like a network failure.
_TRANSIENT_CODES = frozenset({"DV1009", "DV2102", "DV2103", "DV2104"})

_runtime: ImouRuntime | None = None


@dataclass
class _Fail:
    """What is remembered about a camera whose last start failed (module-wide, by camera id)."""

    kind: str            # "retry": a delayed automatic retry is due; "blocked": wait for a click
    message: str
    next_at: float = 0.0  # time.monotonic() of the earliest retry (kind "retry")
    until: float = 0.0    # runtime-clock epoch after which a "blocked" memory is dropped (00:00 UTC), 0 = a click


_memo_lock = threading.Lock()
_fails: dict[str, _Fail] = {}
_streaks: dict[str, int] = {}                 # retries used in the current streak, by camera id
_starts: dict[str, tuple[str, int]] = {}      # camera id -> (UTC day, automatic starts that day)


def _forget(camera_id: str) -> None:
    """A click: forget the failure and the retry count of this camera."""
    with _memo_lock:
        _fails.pop(camera_id, None)
        _streaks.pop(camera_id, None)


def _count_auto_start(camera_id: str, now: float) -> bool:
    """Count one automatic start of ``camera_id`` today; False when today's allowance is used."""
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    with _memo_lock:
        seen_day, count = _starts.get(camera_id, (day, 0))
        if seen_day != day:
            count = 0
        if count >= AUTO_STARTS_PER_DAY:
            return False
        _starts[camera_id] = (day, count + 1)
        return True


def set_runtime(runtime: ImouRuntime | None) -> None:
    """Set (connected) or clear (disconnected) the process-wide iMou runtime. Every failure memory is dropped."""
    global _runtime
    with _memo_lock:
        _runtime = runtime
        _fails.clear()
        _streaks.clear()
        _starts.clear()


def get_runtime() -> ImouRuntime | None:
    return _runtime


def _is_transient(exc: BaseException) -> bool:
    """Only a network failure or a "busy / no answer" code is worth a delayed retry; every other exception is a refusal."""
    if isinstance(exc, ImouConnectionError):
        return True
    return (isinstance(exc, ImouVendorError) and not isinstance(exc, (ImouRateError, ImouLiveExistsError))
            and exc.code in _TRANSIENT_CODES)


class ImouUnsupportedError(TapoError):
    """The requested action does not exist for iMou cameras (yet)."""

    def __init__(self, what: str = ""):
        super().__init__(f"{what}: {IMOU_UNSUPPORTED}" if what else IMOU_UNSUPPORTED)


def _no(what: str):
    raise ImouUnsupportedError(what)


class ImouCameraService(CameraService):
    supports_live_view = True
    supports_ptz = False
    supports_controls = False  # privacy, day/night, LED, alarm, motion detection, presets
    supports_playback = True   # memory-card recordings: a list per day and playback (no downloads, 1x only)
    supports_events = False
    supports_rename = False    # a local name only; nothing is written to the camera or the iMou account

    def __init__(self, config: CameraConfig, vault: Any = None):
        self.config = config
        self._vault = vault
        self._lock = threading.RLock()
        self._gen = 0  # bumps on every start and stop: callbacks of an older run are dropped
        self._sup: Any = None
        self._live_token: str | None = None
        self._started_at = 0.0
        self._timer: threading.Timer | None = None        # the daily minutes of the running stream
        self._retry_timer: threading.Timer | None = None  # the pending automatic retry (never a midnight timer)
        self._on_state: Callable[[StreamState, str | None], None] | None = None
        self._args: tuple | None = None   # (quality, width, height, on_frame, on_state) of the tile being shown
        self._auto = False                # the running / last attempt was an automatic one
        self._playing_since = 0.0         # runtime-clock time of the first frame of the running stream
        self._pb = ImouPlayback(self)
        self._runs_by_day: dict[date, list] = {}  # vendor runs of the days listed so far (their own time texts)
        self.stream_type = "0"                    # stream type of the listed recordings (main stream when present)

    def __repr__(self) -> str:
        return f"ImouCameraService(id={self.config.id!r})"

    @property
    def serial(self) -> str:
        return str(self.config.vendor_data.get("serial") or self.config.device_id)

    @property
    def channel(self) -> str:
        return str(self.config.vendor_data.get("channel") or "0")

    def runtime(self) -> ImouRuntime | None:
        return get_runtime()

    def runtime_or_raise(self) -> ImouRuntime:
        rt = get_runtime()
        if rt is None:
            raise ImouError(texts.NOT_CONNECTED)
        return rt

    def is_online(self, rt: ImouRuntime) -> bool:
        """The online state of the last list call (a Refresh), else the one this camera was built with."""
        return rt.online_of(self.config.id, self.config.online)

    # ================================================================== connect / live view
    def connect(self) -> DeviceInfo:
        return DeviceInfo(model=self.config.model, alias=self.config.name, firmware="", device_id=self.config.device_id)

    def start_stream(self, quality: StreamQuality, width: int, height: int, on_frame, on_state, *,
                     preempt: bool = False) -> None:
        """Start (or switch) the live view of a visible tile. Without ``preempt`` it is an automatic start (the grid's
        plan): skipped, with the remembered text shown again, while a refusal is remembered or a retry is pending.
        ``preempt`` is the controller's "the user clicked the tile": it starts at once and forgets earlier failures."""
        note = ""
        with self._lock:
            self._args = (quality, int(width), int(height), on_frame, on_state)
            self._on_state = on_state
            if self._pb.active:  # one stream per camera, playback wins: nothing is contacted, nothing is remembered
                raise TapoError(texts.PLAYING_TEXT)
            if preempt:
                self._cancel_retry()
                _forget(self.config.id)
            else:
                note = self._note_to_show()
        if note:
            on_state(StreamState.FAILED, note)
            return
        self._begin(auto=not preempt, caller=True)

    def stop_stream(self) -> None:
        with self._lock:
            self._args = None
            self._cancel_retry()
            self._stop_locked(release=True)

    def close(self) -> None:
        self.stop_stream()
        self.stop_playback()

    # ================================================================== one start attempt
    def _note_to_show(self) -> str:
        """The text to show instead of starting ("" = start now). Caller holds ``self._lock``."""
        cid = self.config.id
        with _memo_lock:
            fail = _fails.get(cid)
        if fail is None:
            return ""
        if fail.kind == "blocked":
            rt = get_runtime()
            if fail.until and rt is not None and rt.clock() >= fail.until:  # the UTC day changed: the memory is stale
                with _memo_lock:
                    _fails.pop(cid, None)
                return ""
            return fail.message
        wait = fail.next_at - time.monotonic()
        if wait <= 0:
            return ""
        if self._retry_timer is None:
            self._arm_retry(wait)
        return fail.message

    def _begin(self, *, auto: bool, caller: bool) -> None:
        """One start attempt. A failure that waits for a click is raised to the caller (the controller shows it); a
        failure with a retry pending, and every failure of a timer thread, is reported through the tile callback."""
        failure: Exception | None = None
        with self._lock:
            args = self._args
            if args is None or self._pb.active:
                return
            on_state = args[4]
            self._cancel_retry()
            try:
                self._start_locked(args, auto)
                with _memo_lock:
                    _fails.pop(self.config.id, None)  # a retry that worked: the count stays until a stable run
                return
            except Exception as exc:
                failure = exc
                text, retry = self._remember(exc, auto)
        if caller and not retry:
            raise failure
        on_state(StreamState.FAILED, text)

    def _start_locked(self, args: tuple, auto: bool) -> None:
        quality, width, height, on_frame, on_state = args
        rt = get_runtime()
        if rt is None:
            raise ImouError(texts.NOT_CONNECTED)
        cid = self.config.id
        self._stop_locked(release=False)  # a quality switch keeps the slot and starts a fresh address
        try:
            limits = rt.limits()
            if not self.is_online(rt):
                raise ImouOfflineError(texts.OFFLINE_IN_LIST)
            rt.guard.check()
            remaining = rt.meter.remaining(cid, limits.daily_seconds)
            if remaining <= 0:
                raise ImouBudgetError(texts.budget_used(limits.daily_seconds, rt.clock()))
            if not rt.try_acquire(cid, limits.max_live, "live"):
                raise ImouError(texts.cap_reached(limits.max_live))
            if auto and not _count_auto_start(cid, rt.clock()):
                raise ImouBudgetError(texts.AUTO_LIMIT_TEXT)
            gen = self._gen
            self._auto = auto
            on_state(StreamState.CONNECTING, texts.DELAY_TEXT)
            url, kind = self._resolve(rt, quality)
            sup = rt.supervisor_factory(
                url=url, kind=kind, width=width, height=height, hwaccel=decoder_for(quality),
                on_frame=on_frame, on_state=lambda state, reason, g=gen: self._supervisor_state(g, state, reason),
                stall_timeout=limits.stall_timeout)
            self._sup, self._started_at, self._playing_since = sup, rt.clock(), 0.0
            self._arm_timer(rt, limits, remaining, gen)
            sup.start()
        except BaseException:
            self._finish_locked(rt, delete_live=True)
            raise

    def _remember(self, exc: Exception, auto: bool) -> tuple[str, bool]:
        """Remember a failed start (caller holds the lock). Returns (the text for the tile, whether a retry is pending)."""
        rt = get_runtime()
        cid = self.config.id
        if isinstance(exc, ImouError):
            text = str(exc)
        else:
            log.warning("imou: the live start failed (%s)", type(exc).__name__)
            text = PREFIX + "could not start the live view."
        if isinstance(exc, ImouAuthError) and rt is not None and rt.on_auth_error is not None:
            try:
                rt.on_auth_error()
            except Exception as cb_exc:
                log.warning("imou: the auth-error callback failed (%s)", type(cb_exc).__name__)
        if isinstance(exc, ImouBudgetError):  # the daily minutes or the day's automatic starts: until 00:00 UTC
            until = next_utc_midnight(rt.clock()) if rt is not None else 0.0
            self._store_blocked(text, until)
            return text, False
        if auto and _is_transient(exc):
            shown = self._plan_retry(text, AUTO_RETRY_DELAY_S, count=True)
            if shown is not None:
                return shown, True
        self._store_blocked(text, 0.0)
        return text, False

    def _store_blocked(self, text: str, until: float) -> None:
        with _memo_lock:
            _fails[self.config.id] = _Fail("blocked", text, until=until)

    def _plan_retry(self, text: str, delay: float, *, count: bool) -> str | None:
        """Plan an automatic retry after ``delay`` (caller holds the lock). Returns the tile text, or ``None`` when the
        retries of this streak are used (``count=False``: the quick restart, which is not one of them)."""
        cid = self.config.id
        with _memo_lock:
            used = _streaks.get(cid, 0)
            if count:
                if used >= AUTO_RETRY_MAX:
                    return None
                _streaks[cid] = used + 1
            shown = texts.retrying(text, delay)
            _fails[cid] = _Fail("retry", shown, next_at=time.monotonic() + delay)
        self._arm_retry(delay)
        return shown

    def _arm_retry(self, delay: float) -> None:
        """Caller holds the lock. The timer does nothing when the tile left the view, the run changed or a recording plays."""
        self._cancel_retry()
        timer = threading.Timer(max(0.0, delay), self._on_retry, args=(self._gen,))
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _cancel_retry(self) -> None:
        timer, self._retry_timer = self._retry_timer, None
        if timer is not None:
            timer.cancel()

    def _on_retry(self, gen: int) -> None:
        with self._lock:
            self._retry_timer = None
            if gen != self._gen or self._args is None or self._sup is not None or self._pb.active:
                return
        self._begin(auto=True, caller=False)

    # ================================================================== internals
    def _resolve(self, rt: ImouRuntime, quality: StreamQuality) -> tuple[str, str]:
        serial = str(self.config.vendor_data.get("serial") or self.config.device_id)
        channel = str(self.config.vendor_data.get("channel") or "0")
        stream = 0 if quality in _HD else 1
        try:
            return rt.client.create_rtsp_url(serial, channel, stream), "rtsp"
        except ImouError as exc:
            refused = isinstance(exc, ImouProtocolError) or (isinstance(exc, ImouVendorError) and exc.code in RTSP_REFUSED_CODES)
            if not refused:
                raise
            log.info("imou: the RTSP address was refused (%s); trying the HLS live address", exc.code or "reply")
        try:
            live = rt.client.bind_live(serial, channel, stream)
        except ImouLiveExistsError:
            live = rt.client.find_live(serial, channel, stream)
            if live is None:
                raise
        self._live_token = live.live_token
        return live.hls, "hls"

    def _arm_timer(self, rt: ImouRuntime, limits, remaining: float, gen: int) -> None:
        """The only timer of a live stream: the day's minutes of this camera (no run limit for live view)."""
        text = texts.budget_used(limits.daily_seconds, rt.clock())
        timer = threading.Timer(remaining, self._timeout, args=(gen, text))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _timeout(self, gen: int, text: str) -> None:
        self._end(gen, text, budget=True)

    def _supervisor_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        """Supervisor thread: forward what the tile needs; a failed stream ends the run."""
        on_state = self._on_state
        if gen != self._gen or on_state is None:  # read without the lock: a stop() holds it while it joins us
            return
        if state is StreamState.FAILED or state is StreamState.STOPPED:
            ended_clean = state is StreamState.STOPPED or not reason or reason == "the stream ended" \
                or str(reason).startswith("ffmpeg exited (code 0)")
            self._end(gen, PREFIX + (clean(reason or "") or "the stream ended"), ended_clean=ended_clean)
        elif state is StreamState.CONNECTING:
            on_state(state, texts.DELAY_TEXT)
        else:
            if state is StreamState.PLAYING and not self._playing_since:
                rt = get_runtime()
                self._playing_since = rt.clock() if rt is not None else 0.0
            on_state(state, None if state is StreamState.PLAYING else clean(reason or "") or None)

    def _end(self, gen: int, text: str, *, ended_clean: bool = False, budget: bool = False) -> None:
        """The run ended by itself (failure, daily minutes): clean up, plan what comes next and tell the tile."""
        rt = get_runtime()
        with self._lock:
            if gen != self._gen:
                return
            on_state = self._on_state
            played = rt.clock() - self._playing_since if rt is not None and self._playing_since else 0.0
            self._gen += 1
            sup, self._sup = self._sup, None
            shown = text
            if rt is not None:
                self._finish_locked(rt, delete_live=True, sup=sup)
                if budget:
                    self._store_blocked(text, next_utc_midnight(rt.clock()))
                else:
                    shown = self._after_lost_stream(text, played, ended_clean)
        if on_state is not None:
            on_state(StreamState.FAILED, shown)

    def _after_lost_stream(self, text: str, played: float, ended_clean: bool) -> str:
        """Caller holds the lock. A stream that played a stable minute and ended cleanly restarts quickly (not a retry);
        otherwise an automatic run is retried up to the cap; anything else waits for a click."""
        cid = self.config.id
        stable = played >= STABLE_PLAY_S
        if stable:
            with _memo_lock:
                _streaks[cid] = 0
        if self._args is not None:
            if stable and ended_clean:
                shown = self._plan_retry(text, AUTO_QUICK_DELAY_S, count=False)
            elif self._auto:
                shown = self._plan_retry(text, AUTO_RETRY_DELAY_S, count=True)
            else:
                shown = None
            if shown is not None:
                return shown
        self._store_blocked(text, 0.0)
        return text

    def _stop_locked(self, *, release: bool) -> None:
        rt = get_runtime()
        self._gen += 1
        sup, self._sup = self._sup, None
        if rt is None:
            self._cancel_timer()
            self._stop_supervisor(sup)
            return
        self._finish_locked(rt, delete_live=True, sup=sup, release=release)

    def _finish_locked(self, rt: ImouRuntime, *, delete_live: bool, sup: Any = None, release: bool = True) -> None:
        """Cancel the timer, stop the supervisor, count the minutes, delete a cloud live address, free the slot."""
        self._cancel_timer()
        sup = sup if sup is not None else self._sup
        self._sup = None
        self._playing_since = 0.0
        self._stop_supervisor(sup)
        if self._started_at:
            rt.meter.add(self.config.id, max(0.0, rt.clock() - self._started_at))
            self._started_at = 0.0
        token, self._live_token = self._live_token, None
        if token and delete_live:
            try:
                rt.client.unbind_live(token)
            except Exception as exc:  # best effort: the address expires by itself; never block a stop
                log.info("imou: could not delete a live address (%s)", type(exc).__name__)
        if release:
            rt.release(self.config.id, "live")

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _stop_supervisor(sup: Any) -> None:
        if sup is not None:
            try:
                sup.stop()
            except Exception as exc:
                log.warning("imou: stopping a stream failed (%s)", type(exc).__name__)

    def playback_ended(self) -> None:
        """A recording of this camera ended (window closed, last clip, failure). The live tile, if it is still in view and
        idle, gets one text: the click text, or the budget / guard text when those are why a click would not help. Live
        view does not come back by itself (the minutes were not meant for it) and nothing is sent to the cloud."""
        with self._lock:
            args, rt = self._args, get_runtime()
            if args is None or rt is None or self._sup is not None or self._pb.active:
                return
            text = texts.CLICK_TEXT
            if rt.meter.remaining(self.config.id, rt.limits().daily_seconds) <= 0:
                text = texts.budget_used(rt.limits().daily_seconds, rt.clock())
            else:
                try:
                    rt.guard.check()
                except ImouBlockedError as exc:
                    text = str(exc)
            on_state = args[4]
        on_state(StreamState.FAILED, text)

    # ================================================================== nothing else exists for iMou (yet)
    def ptz_move(self, dx: int, dy: int) -> None:
        _no("Move")

    def ptz_stop(self) -> None:
        _no("Move")

    def get_presets(self) -> list[Preset]:
        _no("Presets")

    def goto_preset(self, preset_id: str) -> None:
        _no("Presets")

    def get_status(self) -> dict:
        _no("Camera status")

    def set_privacy(self, on: bool) -> None:
        _no("Privacy mode")

    def set_day_night(self, mode: DayNightMode) -> None:
        _no("Day/night mode")

    def set_led(self, on: bool) -> None:
        _no("Status LED")

    def set_alarm(self, on: bool) -> None:
        _no("Alarm")

    def set_motion_detection(self, on: bool) -> None:
        _no("Motion detection")

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        _no("Recordings")

    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path,
                           progress: Callable[[float], None] | None = None) -> pathlib.Path:
        _no("Downloads")

    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        pass  # the controller calls this for every camera with enable_onvif_events; iMou has no ONVIF

    def stop_events(self) -> None:
        pass

    def rename(self, name: str) -> None:
        _no("Renaming on the camera")

    # ================================================================== memory-card recordings (SD)
    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        """The recordings of the PC's calendar ``day`` (one list call per 100 files). No lock is held during the call."""
        rt = self.runtime_or_raise()
        if not self.is_online(rt):
            raise ImouOfflineError(texts.OFFLINE_IN_LIST)
        rt.guard.check()
        clock = clock_for(self.config)
        begin, end = clock.day_range(day)
        found = rt.client.list_local_records(self.serial, self.channel, begin, end)
        runs = build_runs(found.files, clock)
        self._runs_by_day[day] = runs
        if runs:
            self.stream_type = runs[0].stream_type
        notes.set(self.config.id, _list_note(found, two_clocks(found.files)))
        day_start, day_end = pc_day_bounds(day)
        clips = []
        for run in runs:
            start, stop = max(run.start_utc, day_start), min(run.end_utc, day_end)
            if stop > start:
                clips.append(RecordingClip(start=datetime.fromtimestamp(start, timezone.utc),
                                           end=datetime.fromtimestamp(stop, timezone.utc), event_type=1))
        return clips

    def find_run(self, utc: float):
        """The listed run that holds ``utc`` (1 s slack), with the vendor's own time texts, or ``None``."""
        for runs in list(self._runs_by_day.values()):
            for run in runs:
                if run.start_utc - 1 <= utc <= run.end_utc + 1:
                    return run
        return None

    def start_playback(self, start_utc: float, end_utc: float, width: int, height: int, on_frame, on_state,
                       on_position, *, segments: list | None = None) -> None:
        self._pb.start(start_utc, end_utc, width, height, on_frame, on_state, on_position, segments)

    def pause_playback(self) -> None:
        self._pb.pause()

    def resume_playback(self) -> None:
        self._pb.resume()

    def seek_playback(self, utc: float) -> None:
        self._pb.seek(utc)

    def set_playback_speed(self, scale: str) -> None:
        if scale != "1/1":
            raise ValueError("iMou recordings play at 1x only")

    def stop_playback(self) -> None:
        self._pb.stop()

    def claim_slot_for_playback(self, rt: ImouRuntime, cap: int) -> None:
        """Playback of this camera takes its slot; a slot held by this camera's live view is taken over later (after
        the playback address exists), so a refused address leaves the live view alone."""
        with self._lock:
            if rt.owner(self.config.id) is None and not rt.try_acquire(self.config.id, cap, "playback"):
                raise ImouError(texts.cap_reached(cap))

    def take_over_live_for_playback(self, rt: ImouRuntime) -> None:
        """Stop this camera's live view (its tile gets a fixed text, no auto return) and move its slot to playback."""
        with self._lock:
            cid = self.config.id
            owner = rt.owner(cid)
            if owner == "playback":
                return
            if owner is None:
                cap = rt.limits().max_live
                if not rt.try_acquire(cid, cap, "playback"):
                    raise ImouError(texts.cap_reached(cap))
                return
            on_state = self._on_state
            self._gen += 1  # callbacks of the stopped live stream are dropped
            self._finish_locked(rt, delete_live=True, release=False)
            rt.transfer(cid, "live", "playback")
        if on_state is not None:
            on_state(StreamState.FAILED, texts.PLAYING_TEXT)


def _list_note(found, two_clocks_seen: bool) -> str:
    parts = []
    if found.truncated:
        parts.append("iMou: showing only the first recordings of this day.")
    if two_clocks_seen:
        parts.append("iMou: the camera reports two clocks. Check the time of day against the picture.")
    return " ".join(parts)
