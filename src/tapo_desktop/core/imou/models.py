"""Small immutable records returned by the iMou client. Nothing here is secret except ``ImouLive.hls``."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImouDevice:
    serial: str
    name: str
    model: str
    status: str          # "online" | "offline" | "sleep" | "upgrading" | ""
    channel: str = "0"   # first channel (v1 shows one tile per device)

    @property
    def online(self) -> bool:
        return self.status == "online"


@dataclass(frozen=True)
class ImouLive:
    """A cloud live address (HLS). ``hls`` is a secret: anyone who has it can watch the camera."""

    live_token: str
    hls: str = field(repr=False)
    device: str = ""
    channel: str = "0"


@dataclass(frozen=True)
class ImouRecordList:
    """Files of one day's listing; `truncated` = the page cap or the deadline ended it before the day was complete."""

    files: list
    truncated: bool = False
