"""Kami Home data model shared by the sign-in lane, the live-view lane and the UI lane."""
from __future__ import annotations

from dataclasses import dataclass

#: Every Kami camera id in the app is ``kami:<uid>``. Tapo ids (cloud deviceId, uuid4 hex, test ids) never
#: contain ":", so the two id spaces cannot collide.
KAMI_ID_PREFIX = "kami:"


def kami_camera_id(uid: str) -> str:
    """The ``CameraConfig.id`` of the Kami camera ``uid``."""
    if not isinstance(uid, str) or not uid.strip():
        raise ValueError("a Kami camera needs a non-empty uid")
    return f"{KAMI_ID_PREFIX}{uid}"


@dataclass(frozen=True)
class KamiDevice:
    """One camera of the Kami account's device list. Holds no secret (tokens stay in the sign-in lane)."""

    uid: str
    name: str = ""
    model: str = ""
    online: bool = True
    shared: bool = False  # shared to this account by someone else
    region: str = ""  # Kami API region the device belongs to

    @property
    def camera_id(self) -> str:
        return kami_camera_id(self.uid)
