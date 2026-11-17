"""Phase 5 test configuration: re-uses the e2e UI fixtures (``make_env``, ``no_app_quit``) the same way
``tests/phase3/conftest.py`` re-uses the Phase 2 ones (``pytest_plugins`` is not allowed in a non-root
conftest)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.e2e.conftest import make_env, no_app_quit  # noqa: E402,F401 (pytest fixtures are used by name)
