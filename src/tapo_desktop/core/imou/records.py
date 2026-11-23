"""Rows of ``record.file.list`` -> files -> runs (pure code).

A *file* is one row as the vendor sent it (strings kept as given). A *run* merges files that touch (gap at most
:data:`MAX_GAP_S`) so the timeline and the clip list show continuous recording, not one entry per file. Only one stream
type is used: the main stream (``"0"``) when any such row exists, else the lowest stream type present.
Rows that are not objects, have no valid times or end before they begin are dropped; the file name is never kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tapo_desktop.core.imou.clock import ImouClock, parse_wall

MAX_GAP_S = 2.0


@dataclass(frozen=True)
class ImouFile:
    begin: str
    end: str
    local_begin: str
    local_end: str
    stream_type: str = "0"


@dataclass(frozen=True)
class ImouRun:
    begin: str          # the vendor's own string of the first file
    end: str            # the vendor's own string of the last end
    start_utc: float
    end_utc: float
    stream_type: str = "0"


def _stream(value: Any) -> str:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit() else "0"


def parse_files(rows: Any) -> list[ImouFile]:
    """Valid, sorted, de-duplicated files of a list of reply rows."""
    out: dict[tuple[str, str, str], ImouFile] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        begin, end = row.get("beginTime"), row.get("endTime")
        b, e = parse_wall(begin), parse_wall(end)
        if b is None or e is None or e < b:
            continue
        lb, le = row.get("localBeginTime"), row.get("localEndTime")
        if parse_wall(lb) is None or parse_wall(le) is None:
            lb, le = begin, end
        file = ImouFile(begin, end, lb, le, _stream(row.get("streamType")))
        out[(file.begin, file.end, file.stream_type)] = file
    return sorted(out.values(), key=lambda f: (f.begin, f.end, f.stream_type))


def two_clocks(files: list[ImouFile]) -> bool:
    """True when any row reports a device time that differs from its ``beginTime`` (the clock is not certain)."""
    return any(f.local_begin != f.begin or f.local_end != f.end for f in files)


def build_runs(files: list[ImouFile], clock: ImouClock, *, gap_s: float = MAX_GAP_S) -> list[ImouRun]:
    if not files:
        return []
    stream = "0" if any(f.stream_type == "0" for f in files) else min(f.stream_type for f in files)
    runs: list[ImouRun] = []
    for f in (f for f in files if f.stream_type == stream):
        start, end = clock.to_utc(f.begin), clock.to_utc(f.end)
        if start is None or end is None:
            continue
        if runs and start - runs[-1].end_utc <= gap_s:
            last = runs[-1]
            if end > last.end_utc:
                runs[-1] = ImouRun(last.begin, f.end, last.start_utc, end, stream)
        else:
            runs.append(ImouRun(f.begin, f.end, start, end, stream))
    return runs
