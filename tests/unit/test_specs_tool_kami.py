"""Gate 1 for the Kami lanes: separate tree, kami. id prefix, Tapo output unchanged."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "specs_tool.py"


@pytest.fixture()
def st():
    spec = importlib.util.spec_from_file_location("specs_tool_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _item(iid: str, file: str = "a/b.java", line: int = 2) -> dict:
    return {"id": iid, "kind": "constant", "description": "d", "confidence": "verified",
            "evidence": [{"source": "jadx", "file": file, "line": line, "snippet": "x"}]}


def test_kami_items_are_checked_against_their_own_tree(st, tmp_path):
    tree = tmp_path / "sources" / "a"
    tree.mkdir(parents=True)
    (tree / "b.java").write_text("one\ntwo\n", encoding="utf-8")
    ok = st._check({"items": [_item("kami.x.ok")]}, tmp_path / "sources", st.KAMI_PREFIX)
    assert ok == []
    bad_line = st._check({"items": [_item("kami.x.line", line=99)]}, tmp_path / "sources", st.KAMI_PREFIX)
    assert any("out of range" in e for e in bad_line)
    bad_file = st._check({"items": [_item("kami.x.file", file="a/nope.java")]}, tmp_path / "sources", st.KAMI_PREFIX)
    assert any("not found" in e for e in bad_file)


def test_id_prefix_rules(st, tmp_path):
    assert any("must start with" in e for e in st._check({"items": [_item("tapo.x")]}, tmp_path, st.KAMI_PREFIX))
    assert any("must not use" in e for e in st._check({"items": [_item("kami.x")]}, tmp_path))


def test_kami_lanes_are_separate_from_tapo_lanes(st):
    assert not set(st.KAMI_LANES.values()) & set(st.LANES.values())
    assert st.KAMI_MERGED != st.MERGED
