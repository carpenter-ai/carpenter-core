"""Shared fixtures for tests/tool_backends/."""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.db import get_db


@pytest.fixture
def create_arc():
    """Factory fixture to create arcs via arc_manager.

    Returns a callable: create_arc(name="test-arc", parent_id=None, **kwargs) -> arc_id

    The created arc will have any default state entries that arc_manager
    populates (e.g. _escalation_on_exhaust, _retry_policy).  Use
    ``create_bare_arc`` when you need a minimal arc with no auto-state.
    """

    def _create(name="test-arc", parent_id=None, **kwargs):
        return arc_manager.create_arc(name=name, parent_id=parent_id, **kwargs)

    return _create


@pytest.fixture
def create_bare_arc():
    """Factory fixture to create a minimal arc row via direct SQL.

    Returns a callable: create_bare_arc(name="test-arc") -> arc_id

    Unlike ``create_arc``, this inserts only the arcs row with no
    auto-populated state entries, making it suitable for tests that
    assert an exact set of state keys.
    """
    _counter = [0]

    def _create(name="test-arc"):
        _counter[0] += 1
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO arcs (name) VALUES (?)", (name,)
            )
            arc_id = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        return arc_id

    return _create
