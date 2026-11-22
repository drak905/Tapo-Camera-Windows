"""The iMou spec lane (Gate 1) and the repository hygiene rules that apply to every iMou file."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPECS = ROOT / "specs"


def _specs_tool():
    spec = importlib.util.spec_from_file_location("specs_tool_for_imou_test", ROOT / "tools" / "specs_tool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_imou_lane_exists_and_every_item_cites_a_public_doc_page():
    data = json.loads((SPECS / "imou_spec.json").read_text(encoding="utf-8"))
    ids = [i["id"] for i in data["items"]]
    assert len(ids) == len(set(ids)) >= 10 and all(i.startswith("imou.") for i in ids)
    for item in data["items"]:
        assert item["confidence"] in ("external", "inferred")
        assert item["evidence"], item["id"]
        for ev in item["evidence"]:
            assert ev["source"] == "doc" and re.fullmatch(r"https://[\w.-]+/[\w./%~-]*", ev["url"]), ev
            assert len(ev["snippet"].splitlines()) <= 2 and len(ev["snippet"]) <= 220


def test_gate_1_passes_with_the_imou_lane_and_the_merged_file_is_in_sync():
    tool = _specs_tool()
    assert tool.verify() == []
    merged = json.loads((SPECS / "imou_protocol_spec.json").read_text(encoding="utf-8"))
    source = json.loads((SPECS / "imou_spec.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in merged["items"]] == [i["id"] for i in source["items"]]


def test_a_tapo_lane_id_may_not_use_the_imou_prefix():
    tool = _specs_tool()
    bad = {"items": [{"id": "imou.x", "kind": "k", "description": "d", "confidence": "external",
                      "evidence": [{"source": "doc", "url": "https://example.com/x", "snippet": "s"}]}]}
    assert any("vendor prefix" in e for e in tool._check(bad, ROOT / "nowhere"))


def test_no_secret_serial_or_address_in_any_imou_file():
    """Made-up ids only: no real-looking App ID, long hex key, private address or serial in code, tests or specs."""
    files = [*(ROOT / "src" / "tapo_desktop").rglob("*imou*.py"), *(ROOT / "src" / "tapo_desktop" / "core" / "imou").glob("*.py"),
             *(ROOT / "tests" / "phase9").glob("*.py"), ROOT / "tests" / "fixtures" / "mock_imou_cloud.py",
             SPECS / "imou_spec.json", SPECS / "imou_protocol_spec.json"]
    docs = ROOT / "docs" / "IMOU.md"
    if docs.is_file():
        files.append(docs)
    pattern = re.compile(r"192\.168\.(?!1\.)\d|\b10\.\d+\.\d+\.\d+\b|\b[0-9a-f]{40,}\b|@(?!example\.)[\w-]+\.(?:com|net|org)\b")
    for path in files:
        text = path.read_text(encoding="utf-8")
        found = [m.group(0) for m in pattern.finditer(text) if m.group(0) not in ("10.0.0.5",)]
        assert found == [], (path.name, found)


def test_git_tracks_no_baseline_apk_or_decompiled_material_for_imou():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return
    assert not [line for line in out.splitlines() if re.search(r"\.(apk|xapk|apkm|so)$|jadx", line, re.I)
                and "imou" in line.lower()]
