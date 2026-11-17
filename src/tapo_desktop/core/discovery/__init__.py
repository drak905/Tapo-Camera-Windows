"""LAN discovery of Tapo cameras (TDP) and cloud<->LAN matching [spec:tdp.*] [spec:lan.match_rule]."""
from tapo_desktop.core.discovery.matcher import match_devices, normalise_mac
from tapo_desktop.core.discovery.tdp import TDP_PORT, build_probe, discover, parse_response

__all__ = ["TDP_PORT", "build_probe", "discover", "match_devices", "normalise_mac", "parse_response"]
