"""Only one Tapo Desktop per Windows user: a second start activates the first window and exits
(several instances would compete for the cameras' ~2 media sessions)."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.ui


def test_second_instance_is_refused_and_first_is_activated(qtbot):
    from tapo_desktop.ui.single_instance import SingleInstance

    name = f"tapo-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    assert first.acquire() is True
    activated: list[bool] = []
    first.activated.connect(lambda: activated.append(True))
    second = SingleInstance(name)
    assert second.acquire() is False  # notifies the first instance
    qtbot.waitUntil(lambda: bool(activated), timeout=3000)
    first.release()
    third = SingleInstance(name)
    assert third.acquire() is True  # free again after release
    third.release()


def test_default_name_is_per_user():
    from tapo_desktop.ui.single_instance import default_instance_name

    assert default_instance_name().startswith("TapoDesktop-")
    assert default_instance_name() == default_instance_name()
