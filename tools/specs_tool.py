"""Dev-time helper: merge Wave 1 specs and run Gate 1 (schema & evidence parity).

Usage:
    python tools/specs_tool.py merge     # writes specs/tapo_protocol_spec.json
    python tools/specs_tool.py verify    # Gate 1; exit code 0 = pass
    python tools/specs_tool.py merge-kami  # writes specs/kami_protocol_spec.json only (Tapo file untouched)
    python tools/specs_tool.py merge-imou  # writes specs/imou_protocol_spec.json only (other files untouched)

Gate 1 rules
  * every item has id, kind, description, confidence in {verified, inferred, external}
  * ids are unique across all lane specs
  * every item has >= 1 evidence entry
  * "verified" items must cite APK evidence (jadx file or rea command)
  * when the JADX tree is present, every cited jadx file must exist and the cited
    line must be within the file (evidence is re-checked against the decompiled APK)

Kami lanes (additive, separate vendor)
  * lanes in KAMI_LANES merge to specs/kami_protocol_spec.json, never into the Tapo file
  * every Kami id starts with "kami."; no Tapo id may start with it
  * Kami jadx evidence is re-checked against the tree named by the KAMI_JADX env var
    (the "sources" folder of a Kami decompile) or artifacts/jadx_kami/sources; the Tapo
    tree is never used for Kami items
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
JADX = ROOT / "artifacts" / "jadx_base" / "sources"
LANES = {
    "auth": "auth_handshake_spec.json",
    "control": "camera_control_dtos.json",
    "media": "stream_profiles.json",
    "vod": "sd_vod_playback_spec.json",
    "cloud": "cloud_account_spec.json",
    "tpap": "tpap_spec.json",
    "lat": "lat_spec.json",
    "remote": "remote_spec.json",
    "remote_control": "remote_control_spec.json",
    "rename": "rename_spec.json",
}
MERGED = SPECS / "tapo_protocol_spec.json"
KAMI_LANES = {
    "auth": "kami_auth_spec.json",
    "cloud": "kami_cloud_spec.json",
    "media": "kami_media_spec.json",
    "control": "kami_control_spec.json",
    "native": "kami_native_spec.json",
}
KAMI_MERGED = SPECS / "kami_protocol_spec.json"
KAMI_PREFIX = "kami."
# iMou Open Platform (official public API, route A): documentation evidence only, no app decompile
IMOU_LANES = {"api": "imou_spec.json"}
IMOU_MERGED = SPECS / "imou_protocol_spec.json"
IMOU_PREFIX = "imou."
IMOU_NO_JADX = ROOT / "artifacts" / "jadx_imou" / "sources"  # never exists: iMou items cite documentation, not a decompile
CONFIDENCE = {"verified", "inferred", "external"}


def kami_jadx() -> Path:
    env = os.environ.get("KAMI_JADX")
    return Path(env) if env else ROOT / "artifacts" / "jadx_kami" / "sources"


def _load(name: str) -> dict:
    return json.loads((SPECS / name).read_text(encoding="utf-8"))


def _merge(lanes: dict, spec_id: str, out: Path) -> dict:
    merged: dict = {
        "spec_id": spec_id,
        "spec_version": 1,
        "apk": None,
        "lanes": {},
        "items": [],
        "unresolved": [],
    }
    for lane, name in lanes.items():
        d = _load(name)
        merged["apk"] = merged["apk"] or d.get("apk")
        merged["lanes"][lane] = {"source_file": f"specs/{name}", "summary": d.get("summary"),
                                 "item_count": len(d.get("items", []))}
        for it in d.get("items", []):
            merged["items"].append({**it, "lane": lane})
        for u in d.get("unresolved", []):
            merged["unresolved"].append({**(u if isinstance(u, dict) else {"detail": u}), "lane": lane})
    out.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def merge() -> dict:
    return _merge(LANES, "tapo_protocol_spec", MERGED)


def merge_kami() -> dict:
    return _merge(KAMI_LANES, "kami_protocol_spec", KAMI_MERGED)


def merge_imou() -> dict:
    return _merge(IMOU_LANES, "imou_protocol_spec", IMOU_MERGED)


def _check(data: dict, jadx: Path, prefix: str | None = None) -> list[str]:
    """Gate 1 for one vendor. prefix=KAMI_PREFIX: ids must start with it; None: ids must not."""
    errors: list[str] = []
    seen: set[str] = set()
    line_cache: dict[str, int] = {}
    jadx_present = jadx.is_dir()
    for it in data["items"]:
        iid = it.get("id", "<missing id>")
        if prefix is not None and not str(iid).startswith(prefix):
            errors.append(f"{iid}: id must start with {prefix!r}")
        if prefix is None and str(iid).startswith((KAMI_PREFIX, IMOU_PREFIX)):
            errors.append(f"{iid}: Tapo lane id must not use a vendor prefix ({KAMI_PREFIX!r}, {IMOU_PREFIX!r})")
        for key in ("id", "kind", "description", "confidence"):
            if not it.get(key):
                errors.append(f"{iid}: missing {key}")
        if iid in seen:
            errors.append(f"{iid}: duplicate id")
        seen.add(iid)
        if it.get("confidence") not in CONFIDENCE:
            errors.append(f"{iid}: bad confidence {it.get('confidence')!r}")
        ev = it.get("evidence") or []
        if not ev:
            errors.append(f"{iid}: no evidence")
            continue
        apk_ev = [e for e in ev if isinstance(e, dict) and e.get("source") in ("jadx", "rea", "apktool", "ghidra")]
        if it.get("confidence") == "verified" and not apk_ev:
            errors.append(f"{iid}: verified item without APK evidence")
        if jadx_present:
            for e in ev:
                if not isinstance(e, dict) or e.get("source") != "jadx" or not e.get("file"):
                    continue
                rel = str(e["file"]).replace("/", "\\").lstrip("\\")
                if rel.startswith("sources\\"):
                    rel = rel[len("sources\\"):]
                p = jadx / rel
                if not p.is_file():
                    errors.append(f"{iid}: evidence file not found {e['file']}")
                    continue
                if rel not in line_cache:
                    line_cache[rel] = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
                line = e.get("line")
                if isinstance(line, int) and not (1 <= line <= line_cache[rel]):
                    errors.append(f"{iid}: line {line} out of range for {e['file']} ({line_cache[rel]} lines)")
    return errors


def verify() -> list[str]:
    errors = _check(json.loads(MERGED.read_text(encoding="utf-8")), JADX)
    if KAMI_MERGED.is_file():  # Kami lanes are optional; absent file = Tapo-only output, unchanged
        errors += _check(json.loads(KAMI_MERGED.read_text(encoding="utf-8")), kami_jadx(), KAMI_PREFIX)
    if IMOU_MERGED.is_file():  # iMou lane is optional too; its evidence is documentation, so no JADX tree is read
        errors += _check(json.loads(IMOU_MERGED.read_text(encoding="utf-8")), IMOU_NO_JADX, IMOU_PREFIX)
    return errors


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "merge":
        m = merge()
        print(f"merged {len(m['items'])} items -> {MERGED.relative_to(ROOT)}")
    if cmd in ("merge", "merge-kami") and all((SPECS / n).is_file() for n in KAMI_LANES.values()):
        mk = merge_kami()
        print(f"merged {len(mk['items'])} items -> {KAMI_MERGED.relative_to(ROOT)}")
    if cmd in ("merge", "merge-imou") and all((SPECS / n).is_file() for n in IMOU_LANES.values()):
        mi = merge_imou()
        print(f"merged {len(mi['items'])} items -> {IMOU_MERGED.relative_to(ROOT)}")
    errs = verify()
    if errs:
        print(f"GATE 1 FAIL: {len(errs)} problem(s)")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("GATE 1 PASS")
