"""Shared fixtures for review tests."""

from unittest.mock import patch

import pytest


@pytest.fixture
def mock_reviewer_ai():
    """Patch the AI client and rate limiter used by the review pipeline.

    Yields the mock for ``carpenter.agent.providers.anthropic.call`` so that
    individual tests can set ``mock_call.return_value`` or
    ``mock_call.side_effect`` as needed.  The three rate-limiter patches
    (acquire, update_from_headers, record) are configured with harmless
    defaults and are not exposed because no test needs to inspect them.
    """
    with (
        patch("carpenter.agent.providers.anthropic.call") as mock_call,
        patch("carpenter.agent.rate_limiter.acquire", return_value=True),
        patch("carpenter.agent.rate_limiter.update_from_headers"),
        patch("carpenter.agent.rate_limiter.record"),
    ):
        yield mock_call
