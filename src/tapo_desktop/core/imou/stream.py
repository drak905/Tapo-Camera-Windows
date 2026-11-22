"""Stream supervisor for iMou live view: one attempt, a secret URL that never reaches a log, RTSP or HLS input.

* One attempt only. When the stream ends for any reason other than our own ``stop()``, the state goes to FAILED and
  the tile asks for a click: nothing reconnects by itself (every start costs vendor quota).
* The live URL is a secret ("anyone who has it can watch"). The base class only ever sees an opaque stand-in
  ``imou-live://<kind>/<n>``; the real URL is put into the ffmpeg command line here, and every line ffmpeg prints is
  cleaned of it before the base class logs or reports it.
* RTSP input uses the normal Tapo ffmpeg arguments (TCP transport, small probe). An HLS input gets its own arguments:
  no ``-rtsp_transport`` (ffmpeg rejects it for HTTP) and default probing (HLS needs it).
* ``stall_timeout`` (default 30 s) is the first-frame timeout: iMou live starts with a delay.
"""
from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from tapo_desktop.core.imou.errors import clean
from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState, StreamSupervisor, _Outcome, _Target

__all__ = ["ImouStreamSupervisor", "make_supervisor"]

_COUNTER = itertools.count(1)
_HLS_IO_TIMEOUT_US = "15000000"
_PLACEHOLDER_RTSP = "rtsp://placeholder.invalid/x"


class ImouStreamSupervisor(StreamSupervisor):
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
        super().__init__(f"imou-live://{kind}/{next(_COUNTER)}", width, height, on_frame=on_frame,
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
            args = build_ffmpeg_args(_PLACEHOLDER_RTSP, target.width, target.height, hwaccel=hwaccel)
            args[args.index("-i") + 1] = self._real_url
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
        if not self._stopping.is_set() and not outcome.switched:
            outcome.fatal = True  # one attempt: whatever ended it, the tile asks for a click
            outcome.reason = outcome.reason or "the stream ended"
        return outcome

    def __repr__(self) -> str:
        return f"ImouStreamSupervisor(kind={self._kind!r}, state={self.state.name})"


def make_supervisor(*, url: str, kind: str, width: int, height: int, hwaccel: str,
                    on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None],
                    stall_timeout: float = 30.0, ffmpeg_path: str | None = None) -> ImouStreamSupervisor:
    """The default ``ImouRuntime.supervisor_factory``."""
    return ImouStreamSupervisor(url, kind, width, height, on_frame=on_frame, on_state=on_state, hwaccel=hwaccel,
                                ffmpeg_path=ffmpeg_path, stall_timeout=stall_timeout)
