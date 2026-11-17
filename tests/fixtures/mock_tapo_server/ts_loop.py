"""Endless live MPEG-TS built from a short cached clip (used by the :8800 preview of the mock camera).

The clip is split into *units*: one video access unit (from a video PES start up to the next one)
plus the PSI and audio packets muxed in between. :class:`TsLoop` replays the units forever and
rewrites every loop so the output looks like one continuous live stream:

* PTS / DTS (PES headers) and PCR (adaptation fields) are shifted by ``loop * period`` (33-bit wrap),
  ``period`` = video duration of the clip (last PTS - first PTS + one frame);
* continuity counters are renumbered per PID across loops;
* a non-video PES whose shifted PTS would not move forward is dropped (audio is a few ms longer
  than the video in the generated clip), so timestamps of every PID stay strictly increasing.

The clip starts with an IDR frame (keyframe every ``fps`` frames), so every loop restarts on a
keyframe.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

PACKET = 188
_PTS_MASK = (1 << 33) - 1


@dataclass(frozen=True)
class _Packet:
    pos: int  # offset of the packet in the clip
    pid: int
    has_payload: bool
    pcr: int  # offset (inside the packet) of the 6 PCR bytes, -1 if none
    pts: int  # offset of the 5 PTS bytes, -1 if none
    dts: int  # offset of the 5 DTS bytes, -1 if none
    pes_start: bool  # payload starts a PES packet


@dataclass(frozen=True)
class _Unit:
    packets: tuple[_Packet, ...]
    pts: int  # video PTS (90 kHz) relative to the first video PTS of the clip
    keyframe: bool


@dataclass(frozen=True)
class LiveUnit:
    """One emitted chunk of the live stream."""

    data: bytes
    pts: int  # absolute (rewritten) 90 kHz video PTS of the unit
    elapsed: float  # seconds of stream time since the start of the live stream
    keyframe: bool


def read_pts(b: bytes | bytearray, i: int) -> int:
    return (((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22) | ((b[i + 2] >> 1) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1)


def write_pts(b: bytearray, i: int, value: int) -> None:
    value &= _PTS_MASK
    b[i] = (b[i] & 0xF0) | ((value >> 29) & 0x0E) | 1
    b[i + 1] = (value >> 22) & 0xFF
    b[i + 2] = (((value >> 15) & 0x7F) << 1) | 1
    b[i + 3] = (value >> 7) & 0xFF
    b[i + 4] = ((value & 0x7F) << 1) | 1


def read_pcr_base(b: bytes | bytearray, i: int) -> int:
    return (b[i] << 25) | (b[i + 1] << 17) | (b[i + 2] << 9) | (b[i + 3] << 1) | (b[i + 4] >> 7)


def write_pcr_base(b: bytearray, i: int, base: int) -> None:
    base &= _PTS_MASK
    b[i] = (base >> 25) & 0xFF
    b[i + 1] = (base >> 17) & 0xFF
    b[i + 2] = (base >> 9) & 0xFF
    b[i + 3] = (base >> 1) & 0xFF
    b[i + 4] = ((base & 1) << 7) | (b[i + 4] & 0x7F)


def parse_packet(clip: bytes, pos: int) -> _Packet:
    """Locate PCR / PTS / DTS fields of one TS packet (no copy)."""
    p = clip[pos:pos + PACKET]
    if len(p) != PACKET or p[0] != 0x47:
        raise ValueError(f"no TS sync byte at {pos}")
    pid = ((p[1] & 0x1F) << 8) | p[2]
    pusi = bool(p[1] & 0x40)
    afc = (p[3] >> 4) & 3
    idx, pcr = 4, -1
    if afc & 2:
        alen = p[4]
        if alen and p[5] & 0x10:
            pcr = 6
        idx = 5 + alen
    has_payload = bool(afc & 1) and idx < PACKET
    pts = dts = -1
    pes_start = False
    if has_payload and pusi and p[idx:idx + 3] == b"\x00\x00\x01" and idx + 9 <= PACKET:
        pes_start = True
        flags = p[idx + 7] >> 6
        if flags & 2 and idx + 14 <= PACKET:
            pts = idx + 9
        if flags == 3 and idx + 19 <= PACKET:
            dts = idx + 14
    return _Packet(pos, pid, has_payload, pcr, pts, dts, pes_start)


class TsTemplate:
    """Parsed clip: units, video PID, loop period."""

    def __init__(self, clip: bytes):
        if not clip or len(clip) % PACKET:
            raise ValueError("clip is not a whole number of TS packets")
        self.clip = clip
        packets = [parse_packet(clip, pos) for pos in range(0, len(clip), PACKET)]
        video_pid = next((pk.pid for pk in packets if pk.pes_start and 0xE0 <= clip[pk.pos + self._pes_off(pk) + 3] <= 0xEF),
                         None)
        if video_pid is None:
            raise ValueError("clip has no video PES")
        self.video_pid = video_pid
        groups: list[list[_Packet]] = [[]]
        starts: list[int] = []
        for pk in packets:
            if pk.pid == video_pid and pk.pes_start:
                if starts:
                    groups.append([])
                starts.append(read_pts(clip, pk.pos + pk.pts) if pk.pts >= 0 else -1)
            groups[-1].append(pk)
        if not starts or any(s < 0 for s in starts):
            raise ValueError("video PES without PTS")
        first = starts[0]
        frame = (starts[1] - starts[0]) if len(starts) > 1 else 6000
        self.first_pts = first
        self.frame_duration = frame
        self.period = (starts[-1] - first) + frame
        dropped = self._overlapping_pes(packets)
        self.units = tuple(
            _Unit(tuple(pk for pk in group if pk.pos not in dropped), starts[i] - first,
                  self._is_keyframe(group[0]) or i == 0)
            for i, group in enumerate(g for g in groups if g)
        )

    def _overlapping_pes(self, packets: list[_Packet]) -> set[int]:
        """Positions of packets of non-video PES that would overlap the next loop.

        A PES ends where the next PES of its PID starts (the last one is assumed as long as the one
        before it); PES ending later than ``first PES of the PID + period`` are dropped.
        """
        pes: dict[int, list[tuple[int, list[int]]]] = {}
        for pk in packets:
            if pk.pid == self.video_pid:
                continue
            if pk.pes_start and pk.pts >= 0:
                pes.setdefault(pk.pid, []).append((read_pts(self.clip, pk.pos + pk.pts), [pk.pos]))
            elif pk.pid in pes and pk.has_payload:
                pes[pk.pid][-1][1].append(pk.pos)
        dropped: set[int] = set()
        for entries in pes.values():
            if len(entries) < 2:
                continue
            first = entries[0][0]
            for i, (start, positions) in enumerate(entries):
                end = entries[i + 1][0] if i + 1 < len(entries) else start + (start - entries[i - 1][0])
                if end - first > self.period:
                    dropped.update(positions)
        return dropped

    def _pes_off(self, pk: _Packet) -> int:
        p = self.clip[pk.pos:pk.pos + PACKET]
        afc = (p[3] >> 4) & 3
        return 5 + p[4] if afc & 2 else 4

    def _is_keyframe(self, pk: _Packet) -> bool:
        p = self.clip[pk.pos:pk.pos + PACKET]
        afc = (p[3] >> 4) & 3
        return bool(afc & 2 and p[4] and p[5] & 0x40)  # random_access_indicator


class TsLoop:
    """Infinite iterator of :class:`LiveUnit` built from a :class:`TsTemplate`."""

    def __init__(self, template: TsTemplate):
        self._t = template
        self._loop = 0
        self._index = 0
        self._cc: dict[int, int] = {}
        self._last_pts: dict[int, int] = {}
        self._dropping: set[int] = set()

    def __iter__(self) -> "TsLoop":
        return self

    def __next__(self) -> LiveUnit:
        t = self._t
        unit = t.units[self._index]
        shift = self._loop * t.period
        out = bytearray()
        for pk in unit.packets:
            data = self._rewrite(pk, shift)
            if data is not None:
                out += data
        rel = self._loop * t.period + unit.pts
        live = LiveUnit(bytes(out), (t.first_pts + rel) & _PTS_MASK, rel / 90000.0, unit.keyframe)
        self._index += 1
        if self._index == len(t.units):
            self._index = 0
            self._loop += 1
        return live

    def _rewrite(self, pk: _Packet, shift: int) -> bytearray | None:
        clip = self._t.clip
        b = bytearray(clip[pk.pos:pk.pos + PACKET])
        if pk.pes_start:
            new_pts = (read_pts(b, pk.pts) + shift) if pk.pts >= 0 else None
            if pk.pid != self._t.video_pid and new_pts is not None:
                last = self._last_pts.get(pk.pid)
                if last is not None and new_pts <= last:
                    self._dropping.add(pk.pid)
                    return None
                self._dropping.discard(pk.pid)
            if new_pts is not None:
                self._last_pts[pk.pid] = new_pts
                write_pts(b, pk.pts, new_pts)
            if pk.dts >= 0:
                write_pts(b, pk.dts, read_pts(b, pk.dts) + shift)
        elif pk.pid in self._dropping:
            return None
        if pk.pcr >= 0:
            write_pcr_base(b, pk.pcr, read_pcr_base(b, pk.pcr) + shift)
        if pk.has_payload:
            cc = (self._cc.get(pk.pid, -1) + 1) & 0x0F
            self._cc[pk.pid] = cc
        else:
            cc = self._cc.get(pk.pid, b[3] & 0x0F)
        b[3] = (b[3] & 0xF0) | cc
        return b


_templates: dict[int, TsTemplate] = {}
_lock = threading.Lock()


def template_for(clip: bytes) -> TsTemplate:
    """Parsed template for ``clip`` (cached per clip object)."""
    key = id(clip)
    with _lock:
        cached = _templates.get(key)
        if cached is not None and cached.clip is clip:
            return cached
    template = TsTemplate(clip)
    with _lock:
        _templates[key] = template
    return template
