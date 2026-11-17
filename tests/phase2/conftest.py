"""Phase 2 test configuration: makes the shared Phase 2 fixtures available under tests/phase2/.

The contract asked for ``pytest_plugins = ["tests.fixtures.phase2_fixtures"]``, but pytest >= 8
rejects ``pytest_plugins`` in a non-root conftest when the whole suite is collected ("Defining
'pytest_plugins' in a non-top-level conftest is no longer supported"). Importing the fixtures here
registers the same names (``mock_cloud``, ``mock_tdp``, ``mock_cloud_factory``, ``mock_tdp_factory``)
for every test below this directory and nowhere else.
"""
from tests.fixtures.phase2_fixtures import (  # noqa: F401 (pytest fixtures are used by name)
    mock_cloud,
    mock_cloud_factory,
    mock_tdp,
    mock_tdp_factory,
)
