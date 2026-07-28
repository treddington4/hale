"""P21 sleep awareness — stats.typical_wake_time.

The bug worth pinning is the timezone one. garmin_sync._extract_sleep_stages stores
Garmin's raw `startGMT`/`endGMT` with no conversion, so reading the stored value as a
local clock time is off by the UTC offset. Verified against real production data: this
athlete's stored wake times read ~10:06, but their true local median is 06:06. A naive
threshold check would have called a consistent 6am riser a late sleeper and fired the
warning at exactly the wrong person, every single day.
"""
import json
from datetime import date, timedelta

import pytest

from app import stats
from app.models import DailySteps


@pytest.fixture()
def sleep_night(db, user_id):
    """One night whose final segment ends at the given UTC time."""
    def _make(d: date, end_utc_hhmm: str, stage="light"):
        h, m = end_utc_hhmm.split(":")
        segs = [{"start": f"{d.isoformat()}T00:10:00.0", "end": f"{d.isoformat()}T00:40:00.0", "stage": stage},
                {"start": f"{d.isoformat()}T{h}:00:00.0", "end": f"{d.isoformat()}T{h}:{m}:00.0", "stage": stage}]
        db.add(DailySteps(date=d.isoformat(), user_id=user_id, sleep_stages_json=json.dumps(segs)))
        db.commit()
    return _make


def _recent(n):
    return date.today() - timedelta(days=n)


def test_stored_gmt_is_converted_to_local(db, user_id, sleep_night, monkeypatch):
    """The core regression. 10:29 GMT is 06:29 in US-Eastern — the function must report
    the athlete's real local wake time, not the raw stored GMT clock reading."""
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i in range(1, 8):
        sleep_night(_recent(i), "10:29")
    assert stats.typical_wake_time(db, user_id) == "06:29"


def test_a_real_early_riser_never_trips_the_late_threshold(db, user_id, sleep_night, monkeypatch):
    """End-to-end statement of the bug's consequence, using production's real numbers."""
    from app.coach import generator
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i, hhmm in enumerate(["10:29", "10:46", "10:04", "10:21", "10:06", "10:09", "09:53"], start=1):
        sleep_night(_recent(i), hhmm)
    wake = stats.typical_wake_time(db, user_id)
    assert wake is not None
    assert wake < generator.LATE_WAKE_THRESHOLD, (
        f"a 6am riser read as {wake} and would get a spurious late-sleeper warning")


def test_genuinely_late_sleeper_is_detected(db, user_id, sleep_night, monkeypatch):
    """The guard must still fire for someone it's actually about — 13:30 GMT = 09:30 local."""
    from app.coach import generator
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i in range(1, 8):
        sleep_night(_recent(i), "13:30")
    wake = stats.typical_wake_time(db, user_id)
    assert wake == "09:30"
    assert wake > generator.LATE_WAKE_THRESHOLD


def test_returns_none_below_min_nights(db, user_id, sleep_night, monkeypatch):
    """Never fabricates a habit from two nights."""
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i in range(1, 3):
        sleep_night(_recent(i), "10:00")
    assert stats.typical_wake_time(db, user_id) is None


def test_median_not_mean_resists_one_outlier(db, user_id, sleep_night, monkeypatch):
    """One airport 03:00 wake-up must not drag a stable habit — same argument that
    settled the ramp base's median-vs-mean question."""
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i in range(1, 7):
        sleep_night(_recent(i), "10:00")
    sleep_night(_recent(7), "07:00")  # 03:00 local outlier
    assert stats.typical_wake_time(db, user_id) == "06:00"


def test_weekday_filter_narrows_to_one_day(db, user_id, sleep_night, monkeypatch):
    """Weekend and weekday wake times genuinely differ, and a long run sits on a
    specific day — so the check has to be able to ask about that day alone."""
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    # 8 weeks of Sundays, late; everything else early.
    for i in range(1, 57):
        d = _recent(i)
        sleep_night(d, "14:00" if d.weekday() == 6 else "10:00")
    sunday_wake = stats.typical_wake_time(db, user_id, lookback_days=60, weekday=6)
    overall = stats.typical_wake_time(db, user_id, lookback_days=60)
    assert sunday_wake == "10:00"
    assert overall != sunday_wake


def test_malformed_rows_are_skipped_not_crashed(db, user_id, sleep_night, monkeypatch):
    monkeypatch.setattr(stats, "user_timezone", lambda uid=None: "America/New_York")
    for i in range(1, 7):
        sleep_night(_recent(i), "10:00")
    for i, bad in enumerate(["not json", "[]", '[{"stage":"light"}]'], start=20):
        db.add(DailySteps(date=_recent(i).isoformat(), user_id=user_id, sleep_stages_json=bad))
    db.commit()
    assert stats.typical_wake_time(db, user_id, lookback_days=60) == "06:00"
