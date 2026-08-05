"""Coach temporal context — date/time-of-day/elapsed-time grounding
(coach/core.py's get_date_context_block, get_last_message_at,
get_conversation_replay_block) and the fresh-session replay
(coach/assistant.py's _get_client/send_message).

Motivated by a real complaint: "coach needs to keep message context when you ask a
question" and "messages need to have a date/time associated with them so coach knows
how long since last message". Root cause of the "start of conversation" symptom traced
to assistant._get_client's session cache being in-process memory only -- a container
restart (a redeploy), a crash, or a cache eviction all hand the next message a session
with zero turn-by-turn memory, even though ChatMessage history (and the UI showing it)
is completely intact.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ChatMessage
from app.coach import core as coach


def _add_message(db, user_id, role, content, hours_ago=0.0, is_test=False):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    db.add(ChatMessage(user_id=user_id, role=role, content=content,
                       created_at=when.isoformat(), is_test=is_test))
    db.commit()


# ---------- _format_elapsed ----------


def test_elapsed_phrasing_by_magnitude():
    assert coach._format_elapsed(0.01) == "just now"
    assert "minutes" in coach._format_elapsed(0.5)
    assert coach._format_elapsed(1.0) == "1 hour"
    assert coach._format_elapsed(3.0) == "3 hours"
    assert coach._format_elapsed(30.0) == "30 hours"
    assert coach._format_elapsed(49.0) == "2 days"
    assert coach._format_elapsed(24 * 5) == "5 days"


# ---------- get_date_context_block ----------


def test_date_block_states_current_time_not_just_date(monkeypatch, user_id):
    monkeypatch.setattr(coach, "user_timezone", lambda uid=None: "UTC")
    block = coach.get_date_context_block(user_id)
    assert "Today's date is" in block
    assert "current local time is" in block, "time-of-day must be stated, not just the date"


def test_date_block_reports_elapsed_since_last_message(monkeypatch, user_id):
    monkeypatch.setattr(coach, "user_timezone", lambda uid=None: "UTC")
    then = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    block = coach.get_date_context_block(user_id, last_message_at=then)
    assert "last message" in block
    assert "20 hour" in block


def test_date_block_omits_elapsed_line_with_no_prior_message(monkeypatch, user_id):
    monkeypatch.setattr(coach, "user_timezone", lambda uid=None: "UTC")
    block = coach.get_date_context_block(user_id, last_message_at=None)
    assert "last message" not in block


def test_date_block_never_crashes_on_a_malformed_timestamp(monkeypatch, user_id):
    monkeypatch.setattr(coach, "user_timezone", lambda uid=None: "UTC")
    for bad in ("not a timestamp", "", "2026-99-99"):
        coach.get_date_context_block(user_id, last_message_at=bad)  # must not raise


# ---------- get_last_message_at ----------


def test_last_message_at_is_none_with_no_history(db, user_id):
    assert coach.get_last_message_at(db, user_id) is None


def test_last_message_at_returns_the_most_recent_real_message(db, user_id):
    _add_message(db, user_id, "user", "first", hours_ago=5)
    _add_message(db, user_id, "assistant", "reply", hours_ago=3)
    latest = _add_message(db, user_id, "user", "second", hours_ago=1)
    got = coach.get_last_message_at(db, user_id)
    then = datetime.fromisoformat(got)
    assert (datetime.now(timezone.utc) - then).total_seconds() < 3700  # ~the 1h-ago row


def test_last_message_at_excludes_test_traffic(db, user_id):
    """Verification messages (X-Hale-Test) must never leak into what the model thinks
    is real elapsed conversation time — same is_test discipline as everywhere else."""
    _add_message(db, user_id, "user", "real message", hours_ago=10)
    _add_message(db, user_id, "assistant", "test reply", hours_ago=0.01, is_test=True)
    got = coach.get_last_message_at(db, user_id)
    then = datetime.fromisoformat(got)
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    assert 9 < hours < 11, "a test-tagged message must not count as the real last message"


# ---------- get_conversation_replay_block ----------


def test_replay_block_empty_with_no_history(db, user_id):
    assert coach.get_conversation_replay_block(db, user_id) == ""


def test_replay_block_contains_real_history_with_timestamps(db, user_id, monkeypatch):
    monkeypatch.setattr(coach, "user_timezone", lambda uid=None: "UTC")
    _add_message(db, user_id, "user", "How's my ramp looking?", hours_ago=30)
    _add_message(db, user_id, "assistant", "You're on track for build phase.", hours_ago=29)
    block = coach.get_conversation_replay_block(db, user_id)
    assert "How's my ramp looking?" in block
    assert "You're on track for build phase." in block
    assert "Athlete:" in block and "Coach:" in block


def test_replay_block_excludes_test_traffic(db, user_id):
    _add_message(db, user_id, "user", "REAL_MARKER_MESSAGE", hours_ago=5)
    _add_message(db, user_id, "user", "TEST_MARKER_MESSAGE", hours_ago=1, is_test=True)
    block = coach.get_conversation_replay_block(db, user_id)
    assert "REAL_MARKER_MESSAGE" in block
    assert "TEST_MARKER_MESSAGE" not in block


def test_replay_block_respects_the_limit(db, user_id):
    for i in range(10):
        _add_message(db, user_id, "user", f"message {i}", hours_ago=10 - i)
    block = coach.get_conversation_replay_block(db, user_id, limit=3)
    # Only the 3 most recent should appear.
    assert "message 9" in block and "message 8" in block and "message 7" in block
    assert "message 0" not in block


def test_replay_block_preserves_chronological_order(db, user_id):
    _add_message(db, user_id, "user", "MSG_OLDEST", hours_ago=3)
    _add_message(db, user_id, "assistant", "MSG_MIDDLE", hours_ago=2)
    _add_message(db, user_id, "user", "MSG_NEWEST", hours_ago=1)
    block = coach.get_conversation_replay_block(db, user_id)
    assert block.index("MSG_OLDEST") < block.index("MSG_MIDDLE") < block.index("MSG_NEWEST")


def test_replay_block_truncates_very_long_messages(db, user_id):
    _add_message(db, user_id, "user", "x" * 2000, hours_ago=1)
    block = coach.get_conversation_replay_block(db, user_id)
    assert len(block) < 2000, "a single long message must not blow up the primed context"
