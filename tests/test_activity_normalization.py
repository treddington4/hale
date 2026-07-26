"""Spec for activity-type normalization.

These are the 19 distinct `activity_type` spellings actually present in the real
database, covering ~8 real activities. They arrive raw from three sync sources
with no write-time normalization, so every consumer has to normalize on read —
and today four different normalizers do it four slightly different ways.

This file is the single agreed answer. P2 makes the backend match it; P4 starts
normalizing on write so raw spellings stop entering the table at all.
"""
import pytest

from app.stats import _normalize_activity_type


# (raw spelling as stored, expected canonical type)
CANONICAL_CASES = [
    # run family — Strava uses "Run", Garmin uses the lowercase variants
    ("Run", "run"),
    ("running", "run"),
    ("treadmill_running", "run"),
    ("trail_running", "run"),
    # walk
    ("Walk", "walk"),
    ("walking", "walk"),
    # swim
    ("Swim", "swim"),
    # ride
    ("Ride", "ride"),
    ("cycling", "ride"),
]


@pytest.mark.parametrize("raw,expected", CANONICAL_CASES)
def test_canonical_type(raw, expected):
    assert _normalize_activity_type(raw) == expected


def test_road_biking_normalizes_to_ride():
    assert _normalize_activity_type("road_biking") == "ride"


@pytest.mark.parametrize("raw", ["WeightTraining", "strength_training"])
def test_strength_spellings_unify(raw):
    assert _normalize_activity_type(raw) == "strength"


def test_normalizer_is_total():
    """Never raises and never returns None for any real spelling — every consumer
    treats the result as a plain string."""
    for raw in [
        "Run", "running", "treadmill_running", "trail_running", "Walk", "walking",
        "Swim", "Ride", "cycling", "road_biking", "WeightTraining",
        "strength_training", "Yoga", "yoga", "pilates", "breathwork", "mobility",
        "Workout", "other",
    ]:
        result = _normalize_activity_type(raw)
        assert isinstance(result, str) and result, f"{raw!r} produced {result!r}"


@pytest.mark.parametrize("raw", [None, ""])
def test_normalizer_handles_missing_type(raw):
    """`activity_type` is nullable; normalizing must not crash on a NULL row."""
    assert _normalize_activity_type(raw) == ""
