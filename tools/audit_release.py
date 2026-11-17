"""Gate 4 clean-room release audit.

Fails if the release directory contains dev-time reverse-engineering material:
REA, JADX, Apktool, Ghidra, the Tapo APK/XAPK, decompiled sources, smali, dex, or the
spec/evidence files. Scans file names and (for text-like/small files) contents.

Usage: python tools/audit_release.py dist/TapoDesktop [--json report.json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FORBIDDEN_NAMES = re.compile(
    r"(\.apk$|\.xapk$|\.dex$|\.smali$|jadx|apktool|ghidra|rea-agents|tapo_protocol_spec|"
    r"_spec\.json$|rea_evidence|subagent_reports|libtapocameranetwork|com\.tplink\.iot)", re.I)
# byte patterns that would indicate decompiled/proprietary content or dev tooling inside binaries
FORBIDDEN_BYTES = [b"jadx.core", b"brut.androlib", b"ghidra.app", b"rea-agents", b"com.tplink.libtapocameranetwork",
                   b"import com.tplink."]
SCAN_LIMIT = 64 * 1024 * 1024  # skip content scan above this size (e.g. Qt WebEngine is not bundled anyway)
SKIP_CONTENT = {"ffmpeg.exe"}  # third-party binary, name-checked only


def audit(root: Path) -> dict:
    problems: list[str] = []
    files = [p for p in root.rglob("*") if p.is_file()]
    for p in files:
        rel = p.relative_to(root).as_posix()
        if FORBIDDEN_NAMES.search(rel):
            problems.append(f"forbidden file name: {rel}")
            continue
        if p.name.lower() in SKIP_CONTENT or p.stat().st_size > SCAN_LIMIT:
            continue
        data = p.read_bytes()
        for pat in FORBIDDEN_BYTES:
            if pat in data:
                problems.append(f"forbidden content {pat.decode()!r} in {rel}")
    total = sum(p.stat().st_size for p in files)
    return {"root": str(root), "files": len(files), "bytes": total, "problems": problems,
            "passed": not problems}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    rep = audit(Path(sys.argv[1]))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "problems"}, indent=2))
    for pr in rep["problems"]:
        print("  -", pr)
    print("AUDIT PASS" if rep["passed"] else "AUDIT FAIL")
    sys.exit(0 if rep["passed"] else 1)
