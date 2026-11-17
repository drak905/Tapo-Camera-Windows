"""Dev-time helper: merge Wave 1 specs and run Gate 1 (schema & evidence parity).

Usage:
    python tools/specs_tool.py merge     # writes specs/tapo_protocol_spec.json
    python tools/specs_tool.py verify    # Gate 1; exit code 0 = pass

Gate 1 rules
  * every item has id, kind, description, confidence in {verified, inferred, external}
  * ids are unique across all lane specs
  * every item has >= 1 evidence entry
  * "verified" items must cite APK evidence (jadx file or rea command)
  * when the JADX tree is present, every cited jadx file must exist and the cited
    line must be within the file (evidence is re-checked against the decompiled APK)
"""
from __future__ import annotations

import json
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
}
MERGED = SPECS / "tapo_protocol_spec.json"
CONFIDENCE = {"verified", "inferred", "external"}


def _load(name: str) -> dict:
    return json.loads((SPECS / name).read_text(encoding="utf-8"))


def merge() -> dict:
    merged: dict = {
        "spec_id": "tapo_protocol_spec",
        "spec_version": 1,
        "apk": None,
        "lanes": {},
        "items": [],
        "unresolved": [],
    }
    for lane, name in LANES.items():
        d = _load(name)
        merged["apk"] = merged["apk"] or d.get("apk")
        merged["lanes"][lane] = {"source_file": f"specs/{name}", "summary": d.get("summary"),
                                 "item_count": len(d.get("items", []))}
        for it in d.get("items", []):
            merged["items"].append({**it, "lane": lane})
        for u in d.get("unresolved", []):
            merged["unresolved"].append({**(u if isinstance(u, dict) else {"detail": u}), "lane": lane})
    MERGED.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def verify() -> list[str]:
    errors: list[str] = []
    data = json.loads(MERGED.read_text(encoding="utf-8"))
    seen: set[str] = set()
    line_cache: dict[str, int] = {}
    jadx_present = JADX.is_dir()
    for it in data["items"]:
        iid = it.get("id", "<missing id>")
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
                p = JADX / rel
                if not p.is_file():
                    errors.append(f"{iid}: evidence file not found {e['file']}")
                    continue
                if rel not in line_cache:
                    line_cache[rel] = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
                line = e.get("line")
                if isinstance(line, int) and not (1 <= line <= line_cache[rel]):
                    errors.append(f"{iid}: line {line} out of range for {e['file']} ({line_cache[rel]} lines)")
    return errors


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "merge":
        m = merge()
        print(f"merged {len(m['items'])} items -> {MERGED.relative_to(ROOT)}")
    errs = verify()
    if errs:
        print(f"GATE 1 FAIL: {len(errs)} problem(s)")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("GATE 1 PASS")
