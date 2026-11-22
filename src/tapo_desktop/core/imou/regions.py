"""The three overseas data centers of the iMou Open Platform and the only hosts the client may contact.

The data center is chosen by the user from a fixed dropdown; there is no free URL field and no automatic try of the
other data centers (a signed call with the secret never goes to a host the user did not choose).
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: id -> (host, label shown in the dialog)
DATA_CENTERS: dict[str, tuple[str, str]] = {
    "sg": ("openapi-sg.easy4ip.com", "Singapore (East Asia, sg)"),
    "fk": ("openapi-fk.easy4ip.com", "Frankfurt (Europe, fk)"),
    "or": ("openapi-or.easy4ip.com", "Oregon (North America, or)"),
}
_HOSTS = frozenset(host for host, _label in DATA_CENTERS.values())


def labels() -> list[tuple[str, str]]:
    """``(id, label)`` pairs for a dropdown."""
    return [(dc, label) for dc, (_host, label) in DATA_CENTERS.items()]


def base_url(data_center: object) -> str:
    """``https://openapi-<dc>.easy4ip.com`` for a known data center id; ``ValueError`` for anything else."""
    if not isinstance(data_center, str) or data_center not in DATA_CENTERS:
        raise ValueError("unknown iMou data center")
    return f"https://{DATA_CENTERS[data_center][0]}"


def is_allowed_url(url: str) -> bool:
    """True only for ``https://<one of the three hosts>[:443]/...`` without credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname in _HOSTS and port in (None, 443)
            and parts.username is None and parts.password is None)
