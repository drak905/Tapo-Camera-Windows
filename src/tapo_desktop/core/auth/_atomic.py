"""Small file helpers shared by the vault and the TLS pin store (private to lane-impl-control)."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import tempfile

log = logging.getLogger(__name__)


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Write ``text`` (UTF-8) to ``path`` atomically: temp file in the same directory, fsync,
    then ``os.replace``. The temp file is removed if anything fails; the old file stays intact."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json_object(path: pathlib.Path) -> dict:
    """Return the JSON object stored in ``path``; ``{}`` if missing, unreadable or not an object."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable JSON file %s (%s)", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}
