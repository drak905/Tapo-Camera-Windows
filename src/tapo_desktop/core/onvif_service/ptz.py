"""ONVIF PTZ service wrapper."""
from __future__ import annotations

from tapo_desktop.core.onvif_service.soap import OnvifClient
from tapo_desktop.models import Preset


class OnvifPtz:
    """PTZ control for one media profile (``profile_token=None`` -> first profile of the device)."""

    def __init__(self, client: OnvifClient, profile_token: str | None = None):
        raise NotImplementedError

    def continuous_move(self, pan: float, tilt: float, zoom: float = 0.0, timeout_s: float | None = None) -> None:
        """ContinuousMove with velocities clamped to [-1, 1]; ``timeout_s`` -> xs:duration ``PT<s>S``."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop pan/tilt and zoom."""
        raise NotImplementedError

    def get_presets(self) -> list[Preset]:
        """GetPresets -> presets (id = PresetToken)."""
        raise NotImplementedError

    def goto_preset(self, token: str) -> None:
        """GotoPreset with PresetToken ``token``."""
        raise NotImplementedError
