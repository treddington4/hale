"""Common integration-sync interface (Phase 6.5, prompted by adding Hevy alongside
Strava/Garmin). Before this, routes/sync.py dispatched every sync operation
(has-credential checks, "Sync Now", backlog sync, auto-sync) through an if/elif chain
per source -- fine for two sources, already repetitive at three, and would keep
growing linearly with every future integration (Withings, Google Health, ... per
ProviderCredential's own docstring, which already anticipates more).

This does NOT introduce a new credentials table -- ProviderCredential already is
the generic per-user-per-provider connection table (provider: "strava" | "garmin" |
"hevy" | ...), designed for exactly this. What was missing was a common *behavioral*
interface: each provider module (strava.py, garmin_sync.py, hevy_sync.py) keeps its
own real function names (sync_activities, sync_garmin_activities, sync_hevy_workouts,
...) unchanged -- renaming those would ripple into garmin_import.py and every other
existing call site for no real benefit. Instead, each one gets a thin Integration
entry below that normalizes it into one common shape, so routes/sync.py can look up
INTEGRATIONS[source] and call generic methods instead of branching per source name.

Garmin's backlog sync keeps its own rate-limit-aware retry loop in routes/sync.py as
a deliberate, clearly-marked exception -- it's genuinely different behavior (auto-
retry through Garmin's cooldown backoff), not just a differently-named function, and
forcing it into this uniform shape would either lose that behavior or fake a false
uniformity. Abstract what's actually the same; don't force-fit what isn't.
"""
import os
from dataclasses import dataclass
from typing import Callable, Optional

from ..models import SessionLocal, ProviderCredential
from . import strava
from . import garmin_sync
from . import hevy_sync

# Re-exported by routes/sync.py (which routes/settings.py's Config endpoint in turn
# imports from) -- lives here now since it's specifically "how many activities a
# quick sync pulls," a per-integration behavior, even though Hevy's own event-cursor
# sync has no equivalent concept and ignores it entirely.
SYNC_LIMIT = int(os.environ.get("SYNC_ACTIVITY_LIMIT", "10"))


@dataclass
class Integration:
    name: str
    # Official/documented API (Strava, Hevy) -> safe for the scheduled background
    # auto-sync loop. Unofficial/scraped (Garmin) -> manual-only, per CLAUDE.md.
    auto_sync_eligible: bool
    has_credential: Callable[[str], bool]
    sync_recent: Callable[[str, Optional[Callable]], int]
    sync_all: Callable[[str, Optional[Callable]], int]
    missing_credential_message: str


def _has_access_token(user_id: str, provider: str) -> bool:
    """Shared by Strava and Hevy -- both store their credential as a single opaque
    token string in ProviderCredential.access_token (Strava's OAuth token, Hevy's
    API key), unlike Garmin's separate username+password pair."""
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider=provider).first()
        return bool(cred and cred.access_token)
    finally:
        db.close()


def _has_garmin_credential(user_id: str) -> bool:
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="garmin").first()
        return bool(cred and cred.username and cred.password)
    finally:
        db.close()


INTEGRATIONS: dict[str, Integration] = {
    "strava": Integration(
        name="strava",
        auto_sync_eligible=True,
        has_credential=lambda user_id: bool(strava.get_valid_access_token(user_id)),
        sync_recent=lambda user_id, progress_cb=None: strava.sync_activities(user_id, limit=SYNC_LIMIT, progress_cb=progress_cb),
        sync_all=lambda user_id, progress_cb=None: strava.sync_all_activities(user_id, progress_cb=progress_cb),
        missing_credential_message="Not authenticated with Strava — visit /auth/strava/login first",
    ),
    "garmin": Integration(
        name="garmin",
        # False here still means "not part of the generic INTEGRATIONS-driven
        # _auto_sync loop in routes/sync.py" — but as of P8, Garmin is NOT purely
        # manual anymore: it has its own separate scheduled auto-poll
        # (routes/sync.py's _garmin_auto_poll, registered in main.py), gated by its
        # own cooldown pre-check and quiet-hours logic that the generic loop doesn't
        # have. Kept out of this flag/loop deliberately (see this module's docstring
        # and _garmin_auto_poll's own docstring) rather than flipping this to True.
        auto_sync_eligible=False,
        has_credential=lambda user_id: _has_garmin_credential(user_id),
        sync_recent=lambda user_id, progress_cb=None: garmin_sync.sync_garmin_activities(user_id, limit=SYNC_LIMIT, progress_cb=progress_cb),
        # Deliberately NOT garmin_sync.sync_all_garmin_activities directly -- see
        # this module's own docstring on why the rate-limit retry loop stays a
        # special case in routes/sync.py rather than living behind this generic
        # call. Present here so INTEGRATIONS stays a complete registry (e.g. for
        # has_credential/missing_credential_message lookups), but routes/sync.py's
        # backlog runner branches around this specific entry's sync_all.
        sync_all=lambda user_id, progress_cb=None: garmin_sync.sync_all_garmin_activities(user_id, progress_cb=progress_cb),
        missing_credential_message="Add your Garmin credentials in Settings → Connections to use this",
    ),
    "hevy": Integration(
        name="hevy",
        auto_sync_eligible=True,
        has_credential=lambda user_id: _has_access_token(user_id, "hevy"),
        sync_recent=lambda user_id, progress_cb=None: hevy_sync.sync_hevy_workouts(user_id, progress_cb=progress_cb),
        sync_all=lambda user_id, progress_cb=None: hevy_sync.sync_all_hevy_workouts(user_id, progress_cb=progress_cb),
        missing_credential_message="Add your Hevy API key in Settings → Connections to use this",
    ),
}
