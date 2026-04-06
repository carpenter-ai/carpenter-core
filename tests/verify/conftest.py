"""Test configuration for verify module tests.

Enables verification (disabled by default in the global conftest).
"""

import pytest

from carpenter import config


@pytest.fixture(autouse=True)
def enable_verification(monkeypatch):
    """Enable verification for verify module tests."""
    current = config.CONFIG.copy()
    current["verification"] = {"enabled": True, "threshold": 150}
    monkeypatch.setattr(config, "CONFIG", current)
