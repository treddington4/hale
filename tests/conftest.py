"""Shared pytest fixtures.

CRITICAL ordering constraint: `app.models` builds its `engine`/`SessionLocal` at
IMPORT time from `DB_PATH` (see models.py's module level), so DB_PATH must be
pointed at a throwaway file BEFORE anything imports `app.*`. pytest imports
conftest.py before collecting test modules, so setting it at this module's top
level — above the `from app...` imports — is what makes that ordering hold. Do
not move these imports above the environ assignment.
"""
import os
import tempfile

# Must precede any `app.*` import. See docstring above.
_TMP_DIR = tempfile.mkdtemp(prefix="hale-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test.db")

import pytest  # noqa: E402

from app.models import Base, SessionLocal, engine, Run, User  # noqa: E402

TEST_USER_ID = "test_user"


@pytest.fixture()
def user_id():
    """The seeded test user's id, as a fixture so test modules never need
    `from conftest import ...` (which relies on pytest's implicit sys.path
    insertion and breaks under a different import mode)."""
    return TEST_USER_ID


@pytest.fixture()
def db():
    """A clean database per test — schema created from the live models (never a
    hand-maintained copy of the DDL, so a schema change can't silently drift out
    of sync with what these tests exercise), dropped afterward.

    Deliberately calls create_all() directly rather than models.init_db(): init_db
    also runs the hand-rolled migrations and seeds real defaults (a default user, a
    marathon goal, a recovery tool), none of which belong in a hermetic unit test —
    a seeded Goal in particular would skew any aggregate a test asserts on.
    """
    Base.metadata.create_all(engine)
    session = SessionLocal()
    session.add(User(id=TEST_USER_ID, created_at="2026-01-01T00:00:00+00:00"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def make_activity(db):
    """Factory for seeding one activity row.

    Exposed as a fixture rather than an importable helper so tests never need
    `from conftest import ...`, which depends on pytest's implicit sys.path
    insertion and breaks under a different import mode.

    Defaults describe a plain 5-mile run at 9:00/mi, so a test states only the
    fields it actually cares about — usually just source + activity_type spelling.
    """
    def _make(
        id: str,
        activity_type: str,
        date: str = "2026-07-20",
        source: str = "strava",
        start_time: str = "08:00",
        distance_mi: float = 5.0,
        moving_time_sec: int = 2700,
        **overrides,
    ) -> Run:
        pace = None
        if distance_mi and moving_time_sec:
            pace = moving_time_sec / distance_mi
        row = Run(
            id=id,
            user_id=TEST_USER_ID,
            source=source,
            activity_type=activity_type,
            date=date,
            start_time=start_time,
            name=overrides.pop("name", f"Test {activity_type}"),
            distance_mi=distance_mi,
            moving_time_sec=moving_time_sec,
            elev_gain_ft=overrides.pop("elev_gain_ft", 0.0),
            avg_pace_sec_per_mi=overrides.pop("avg_pace_sec_per_mi", pace),
            **overrides,
        )
        db.add(row)
        db.commit()
        return row

    return _make
