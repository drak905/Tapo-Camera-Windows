"""Stream supervisor for iMou live view: one attempt, a secret URL that never reaches a log, RTSP or HLS input.

* One attempt only. When the stream ends for any reason other than our own ``stop()``, the state goes to FAILED; the
  supervisor never reconnects (a retry needs a new address, every start costs vendor quota). The camera service decides
  whether to start a new one (up to 3 automatic retries, see ``services/imou_camera_service.py``) or to wait for a click.
* The live URL is a secret ("anyone who has it can watch"). The base class only ever sees an opaque stand-in
  ``imou-live://<kind>/<n>``; the real URL is put into the ffmpeg command line here, and every line ffmpeg prints is
  cleaned of it before the base class logs or reports it.
* RTSP input uses the normal Tapo ffmpeg arguments (TCP transport, small probe). An HLS input gets its own arguments:
  no ``-rtsp_transport`` (ffmpeg rejects it for HTTP) and default probing (HLS needs it).
* ``stall_timeout`` (default 30 s) is the first-frame timeout: iMou live starts with a delay.
"""
from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from tapo_desktop.core.imou.errors import clean
from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState, StreamSupervisor, _Outcome, _Target

__all__ = ["ImouPlaybackSupervisor", "ImouStreamSupervisor", "PLAYBACK_FPS", "make_supervisor"]

logger = logging.getLogger(__name__)
_COUNTER = itertools.count(1)
_HLS_IO_TIMEOUT_US = "15000000"
_PLACEHOLDER_RTSP = "rtsp://placeholder.invalid/x"
#: Memory-card playback is read at its own rate (``-re``) and written at this fixed rate, so the position can be counted
#: from frames (``begin + index / PLAYBACK_FPS``) whatever the source frame rate is.
PLAYBACK_FPS = 10


class ImouStreamSupervisor(StreamSupervisor):
    _SCHEME = "imou-live"   # the opaque stand-in address the base class sees
    _FPS: int | None = None
    _REALTIME = False

    def __init__(self, url: str, kind: str, width: int, height: int, *, on_frame: Callable[[Frame], None],
                 on_state: Callable[[StreamState, str | None], None] | None = None, hwaccel: str = "none",
                 ffmpeg_path: str | None = None, stall_timeout: float = 30.0):
        if kind not in ("rtsp", "hls"):
            raise ValueError("kind must be 'rtsp' or 'hls'")
        self._real_url = url
        self._kind = kind
        try:
            self._query = urlsplit(url).query
        except ValueError:
            self._query = ""
        super().__init__(f"{self._SCHEME}://{kind}/{next(_COUNTER)}", width, height, on_frame=on_frame,
                         on_state=on_state, ffmpeg_path=ffmpeg_path, hwaccel=hwaccel, stall_timeout=stall_timeout,
                         max_retries=0)

    # ------------------------------------------------------------------ the secret never leaves this class
    def _redact(self, text: str) -> str:
        for secret in (self._real_url, self._query):
            if secret:
                text = text.replace(secret, "<url>")
        return clean(re.sub(r"\s+", " ", text))

    def _decoder_command(self, exe: str, target: _Target, hwaccel: str | None, audio_url: str | None) -> list[str]:
        if self._kind == "rtsp":
            # build with a placeholder so the library's own debug line never sees the real address
            args = build_ffmpeg_args(_PLACEHOLDER_RTSP, target.width, target.height, hwaccel=hwaccel, fps=self._FPS)
            args[args.index("-i") + 1] = self._real_url
            if self._REALTIME:
                args.insert(args.index("-i"), "-re")
            for flag in ("-probesize", "-analyzeduration"):  # H.265 cameras need real probing; iMou waits anyway
                at = args.index(flag)
                del args[at:at + 2]
            return [exe, *args]
        args = ["-hide_banner", "-nostdin", "-loglevel", "error", "-rw_timeout", _HLS_IO_TIMEOUT_US]
        if hwaccel:
            args += ["-hwaccel", hwaccel]
        args += ["-i", self._real_url, "-map", "0:v:0", "-vf",
                 f"scale={target.width}:{target.height}:force_original_aspect_ratio=decrease,"
                 f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2",
                 "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        return [exe, *args]

    def _classify(self, decoder, outcome: _Outcome, scrub) -> _Outcome:
        return super()._classify(decoder, outcome, lambda text: self._redact(scrub(text)))

    def _attempt(self, exe: str, target: _Target) -> _Outcome:
        outcome = super()._attempt(exe, target)
        retry_in_software = outcome.hw_unusable and not self._hw_disabled  # the base class retries once, at once
        if not self._stopping.is_set() and not outcome.switched and not retry_in_software:
            outcome.fatal = True  # one attempt: whatever ended it, the tile asks for a click
            outcome.reason = outcome.reason or "the stream ended"
        return outcome

    def __repr__(self) -> str:
        return f"ImouStreamSupervisor(kind={self._kind!r}, state={self.state.name})"


class ImouPlaybackSupervisor(ImouStreamSupervisor):
    """A memory-card recording over RTSP: read at its own pace, written at a fixed rate, one attempt only.

    A clean end of the file after at least one frame (ffmpeg exits with code 0) is not a failure: the state goes to
    STOPPED and ``on_clip_end()`` runs once, so the service can chain the next request or report the end of the
    recordings. Anything else that ends the stream is FAILED with the cleaned reason, as for live view.
    """

    _SCHEME = "imou-play"
    _FPS = PLAYBACK_FPS
    _REALTIME = True

    def __init__(self, url: str, width: int, height: int, *, on_frame: Callable[[Frame], None],
                 on_clip_end: Callable[[], None], on_state: Callable[[StreamState, str | None], None] | None = None,
                 hwaccel: str = "none", ffmpeg_path: str | None = None, stall_timeout: float = 30.0):
        self._on_clip_end = on_clip_end
        self._clean_end = False
        super().__init__(url, "rtsp", width, height, on_frame=on_frame, on_state=on_state, hwaccel=hwaccel,
                         ffmpeg_path=ffmpeg_path, stall_timeout=stall_timeout)

    def _watch(self, decoder) -> _Outcome:
        outcome = super()._watch(decoder)
        if outcome.reason == "ffmpeg exited (code None)":
            # stdout reached its end a moment before the process was reaped: wait briefly for the real exit code
            try:
                code = decoder._proc.wait(2.0)
            except Exception:
                code = None
            if code is not None:
                outcome.reason = f"ffmpeg exited (code {code})"
        return outcome

    def _attempt(self, exe: str, target: _Target) -> _Outcome:
        outcome = super()._attempt(exe, target)
        if outcome.played and outcome.fatal and outcome.reason.startswith("ffmpeg exited (code 0)"):
            self._clean_end = True
        return outcome

    def _fail(self, reason: str) -> None:
        if not self._clean_end:
            super()._fail(reason)
            return
        self._release_lease()
        self._set_state(StreamState.STOPPED, "end of clip")
        try:
            self._on_clip_end()
        except Exception as exc:  # the service callback must not break the worker thread
            logger.warning("imou clip-end callback failed (%s)", type(exc).__name__)

    def __repr__(self) -> str:
        return f"ImouPlaybackSupervisor(state={self.state.name})"


def make_supervisor(*, url: str, kind: str, width: int, height: int, hwaccel: str,
                    on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None],
                    stall_timeout: float = 30.0, ffmpeg_path: str | None = None, playback: bool = False,
                    on_clip_end: Callable[[], None] | None = None) -> ImouStreamSupervisor:
    """The default ``ImouRuntime.supervisor_factory`` (``playback=True``: a memory-card recording)."""
    if playback:
        return ImouPlaybackSupervisor(url, width, height, on_frame=on_frame, on_state=on_state,
                                      on_clip_end=on_clip_end or (lambda: None), hwaccel=hwaccel,
                                      ffmpeg_path=ffmpeg_path, stall_timeout=stall_timeout)
    return ImouStreamSupervisor(url, kind, width, height, on_frame=on_frame, on_state=on_state, hwaccel=hwaccel,
                                ffmpeg_path=ffmpeg_path, stall_timeout=stall_timeout)
