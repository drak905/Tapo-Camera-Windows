"""iMou camera service (route A, watch only): live view started by a click, nothing else.

* ``start_stream`` is only ever called after a click (the grid does not auto-start iMou tiles). It takes a live slot
  from the iMou runtime's own cap, checks the daily minutes of this camera, asks the iMou cloud for a temporary RTSP
  address (``createVideoDownloadStreamUrl``; HLS ``bindDeviceLive`` only if that call is refused) and hands it to an
  :class:`~tapo_desktop.core.imou.stream.ImouStreamSupervisor`. SD for grid tiles, HD only in full screen.
* A timer ends the stream after the idle limit (default 10 minutes) or when the day's minutes run out; the tile then
  asks for a click. A stream that ends for any reason is not restarted by itself.
* A cloud HLS address is deleted when the stream stops, fails or the service closes.
* Every other action raises "Not supported for iMou cameras yet"; the capability flags tell the UI to hide them.
* The live URL is a secret: it is never logged and never shown (texts go through ``errors.clean``).
The runtime (client, guard, limits, meter) is set by the iMou account manager once the user has connected.
"""
from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from tapo_desktop.core.errors import TapoError
from tapo_desktop.core.imou import notes, texts
from tapo_desktop.core.imou.clock import clock_for, pc_day_bounds
from tapo_desktop.core.imou.errors import (
    PREFIX, RTSP_REFUSED_CODES, ImouError, ImouLiveExistsError, ImouOfflineError, ImouProtocolError, ImouVendorError,
    clean,
)
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

_runtime: ImouRuntime | None = None


def set_runtime(runtime: ImouRuntime | None) -> None:
    """Set (connected) or clear (disconnected) the process-wide iMou runtime."""
    global _runtime
    _runtime = runtime


def get_runtime() -> ImouRuntime | None:
    return _runtime


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
        self._timer: threading.Timer | None = None
        self._on_state: Callable[[StreamState, str | None], None] | None = None
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

    # ================================================================== connect / live view
    def connect(self) -> DeviceInfo:
        return DeviceInfo(model=self.config.model, alias=self.config.name, firmware="", device_id=self.config.device_id)

    def start_stream(self, quality: StreamQuality, width: int, height: int, on_frame, on_state) -> None:
        rt = get_runtime()
        if rt is None:
            raise ImouError(texts.NOT_CONNECTED)
        cid = self.config.id
        with self._lock:
            if self._pb.active:  # one stream per camera, playback wins: nothing is contacted
                raise TapoError(texts.PLAYING_TEXT)
            self._stop_locked(release=False)  # a quality switch keeps the slot and starts a fresh address
            try:
                limits = rt.limits()
                if not self.config.online:
                    raise ImouOfflineError(texts.OFFLINE_IN_LIST)
                rt.guard.check()
                remaining = rt.meter.remaining(cid, limits.daily_seconds)
                if remaining <= 0:
                    raise ImouError(texts.budget_used(limits.daily_seconds))
                if not rt.try_acquire(cid, limits.max_live, "live"):
                    raise ImouError(texts.cap_reached(limits.max_live))
                gen = self._gen
                self._on_state = on_state
                on_state(StreamState.CONNECTING, texts.DELAY_TEXT)
                url, kind = self._resolve(rt, quality)
                sup = rt.supervisor_factory(
                    url=url, kind=kind, width=width, height=height, hwaccel=decoder_for(quality),
                    on_frame=on_frame, on_state=lambda state, reason, g=gen: self._supervisor_state(g, state, reason),
                    stall_timeout=limits.stall_timeout)
                self._sup, self._started_at = sup, rt.clock()
                self._arm_timer(rt, limits, remaining, gen)
                sup.start()
            except BaseException:
                self._finish_locked(rt, delete_live=True)
                raise

    def stop_stream(self) -> None:
        with self._lock:
            self._stop_locked(release=True)

    def close(self) -> None:
        self.stop_stream()
        self.stop_playback()

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
        budget_first = remaining <= limits.idle_seconds
        delay = remaining if budget_first else limits.idle_seconds
        text = texts.budget_used(limits.daily_seconds) if budget_first else texts.idle_stopped(limits.idle_seconds)
        timer = threading.Timer(delay, self._timeout, args=(gen, text))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _timeout(self, gen: int, text: str) -> None:
        self._end(gen, text)

    def _supervisor_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        """Supervisor thread: forward what the tile needs; a failed stream ends the run."""
        on_state = self._on_state
        if gen != self._gen or on_state is None:  # read without the lock: a stop() holds it while it joins us
            return
        if state is StreamState.FAILED or state is StreamState.STOPPED:
            self._end(gen, PREFIX + (clean(reason or "") or "the stream ended"))
        elif state is StreamState.CONNECTING:
            on_state(state, texts.DELAY_TEXT)
        else:
            on_state(state, None if state is StreamState.PLAYING else clean(reason or "") or None)

    def _end(self, gen: int, text: str) -> None:
        """The run ended by itself (failure, idle limit, daily minutes): clean up and tell the tile."""
        rt = get_runtime()
        with self._lock:
            if gen != self._gen:
                return
            on_state = self._on_state
            self._gen += 1
            sup, self._sup = self._sup, None
            if rt is not None:
                self._finish_locked(rt, delete_live=True, sup=sup)
        if on_state is not None:
            on_state(StreamState.FAILED, text)

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
        if not self.config.online:
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
