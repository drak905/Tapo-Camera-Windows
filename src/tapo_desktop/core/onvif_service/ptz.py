"""ONVIF PTZ service wrapper."""
from __future__ import annotations

import logging
import math
import threading
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from tapo_desktop.core.errors import TapoProtocolError
from tapo_desktop.core.onvif_service import _xml as X
from tapo_desktop.core.onvif_service.soap import OnvifClient, service_path
from tapo_desktop.models import Preset

logger = logging.getLogger(__name__)

_NS = f'xmlns:tptz="{X.TPTZ}" xmlns:tt="{X.TT}"'


def _clamp(value: float) -> float:
    """Clamp a velocity to [-1, 1]; NaN becomes 0."""
    v = float(value)
    return 0.0 if math.isnan(v) else max(-1.0, min(1.0, v))


def _float_attr(el: ET.Element | None, name: str) -> float:
    try:
        v = float(el.get(name, "0")) if el is not None else 0.0
    except ValueError:
        return 0.0
    return v if math.isfinite(v) else 0.0


class OnvifPtz:
    """PTZ control for one media profile (``profile_token=None`` -> first profile of the device).

    The profile token and PTZ service path are resolved lazily on the first command.
    """

    def __init__(self, client: OnvifClient, profile_token: str | None = None):
        self._client = client
        self._profile_token = profile_token
        self._path: str | None = None
        self._lock = threading.Lock()

    def _target(self) -> tuple[str, str]:
        """(service path, escaped profile token), resolving them on first use."""
        with self._lock:
            if self._path is None:
                self._path = service_path(self._client, "ptz")
            if self._profile_token is None:
                profiles = self._client.get_profiles()
                if not profiles:
                    raise TapoProtocolError("camera reports no ONVIF media profiles")
                self._profile_token = str(profiles[0]["token"])
            return self._path, escape(self._profile_token)

    def _send(self, op: str, inner_xml: str) -> ET.Element:
        path, token = self._target()
        body = f"<tptz:{op} {_NS}><tptz:ProfileToken>{token}</tptz:ProfileToken>{inner_xml}</tptz:{op}>"
        return self._client.call(path, body)

    def continuous_move(self, pan: float, tilt: float, zoom: float = 0.0, timeout_s: float | None = None) -> None:
        """ContinuousMove with velocities clamped to [-1, 1]; ``timeout_s`` -> xs:duration ``PT<s>S``.

        The Zoom velocity is only sent when non-zero (Tapo cameras have no optical zoom).
        """
        z = _clamp(zoom)
        velocity = f'<tt:PanTilt x="{X.fmt_number(_clamp(pan))}" y="{X.fmt_number(_clamp(tilt))}"/>'
        if z != 0.0:
            velocity += f'<tt:Zoom x="{X.fmt_number(z)}"/>'
        inner = f"<tptz:Velocity>{velocity}</tptz:Velocity>"
        if timeout_s is not None:
            inner += f"<tptz:Timeout>{X.fmt_duration(timeout_s)}</tptz:Timeout>"
        self._send("ContinuousMove", inner)

    def stop(self) -> None:
        """Stop pan/tilt and zoom."""
        self._send("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>")

    def get_presets(self) -> list[Preset]:
        """GetPresets -> presets (id = PresetToken)."""
        resp = self._send("GetPresets", "")
        presets: list[Preset] = []
        for el in X.iter_local(resp, "Preset"):
            token = (el.get("token") or "").strip()
            if not token:
                continue
            name_el = X.child_local(el, "Name")
            position = X.find_local(el, "PTZPosition")
            pan_tilt = X.find_local(position, "PanTilt") if position is not None else None
            presets.append(Preset(id=token, name=(name_el.text or "").strip() if name_el is not None else "",
                                  pan=_float_attr(pan_tilt, "x"), tilt=_float_attr(pan_tilt, "y")))
        return presets

    def goto_preset(self, token: str) -> None:
        """GotoPreset with PresetToken ``token``."""
        self._send("GotoPreset", f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>")
