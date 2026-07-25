"""Database models. SQLite file lives at /data/runlog.db (mounted volume)."""
from sqlalchemy import (
    create_engine, event, Column, String, Float, Integer, Boolean, Text, ForeignKey,
    UniqueConstraint, or_,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
import os
import uuid

DB_PATH = os.environ.get("DB_PATH", "/data/runlog.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

DEFAULT_USER_ID = "default"


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):
    """SQLite ignores FK constraints per-connection unless this pragma is set — needed
    for the per-user tables' ON DELETE CASCADE (Phase 11, ephemeral demo teardown) to
    actually fire. Inert for every table that already exists in a real, pre-Phase-11
    database: SQLite only enforces constraints actually present in a table's own
    on-disk DDL, and create_all() never retroactively alters an existing table's
    schema — so this only takes effect for tables created fresh going forward (a new
    demo deployment's empty volume), never the production data this app already has."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def owned_by(column, user_id: str):
    """Multi-user filter helper. Existing rows synced before the user_id column existed
    have user_id=NULL (see _migrate_add_missing_columns — this repo's established
    convention is to backfill via a read-time `or` fallback rather than a DB-level
    ALTER TABLE DEFAULT, same pattern already used for the JSON blob columns, e.g.
    `r.recovery_json or "[]"`). NULL rows are treated as belonging to the default user
    (today's actual single-user reality) — only the default user's filter matches them;
    any other real user_id matches exact rows only, never NULLs."""
    if user_id == DEFAULT_USER_ID:
        return or_(column == DEFAULT_USER_ID, column.is_(None))
    return column == user_id


class Run(Base):
    __tablename__ = "runs"

    id = Column(String, primary_key=True)  # e.g. "strava_19268494216" or "garmin_..."
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by() — NULL on pre-migration rows, treated as "default"
    source = Column(String)                # "strava" | "garmin"
    activity_type = Column(String, default="Run")  # raw source activity type: "Run", "Ride", "Walk", "Hike", ...
    date = Column(String)                  # YYYY-MM-DD
    start_time = Column(String)            # HH:MM local
    name = Column(String)
    distance_mi = Column(Float)
    moving_time_sec = Column(Integer)
    elev_gain_ft = Column(Float)
    avg_hr = Column(Integer, nullable=True)
    max_hr = Column(Integer, nullable=True)
    avg_cadence = Column(Float, nullable=True)   # true steps/min (already doubled)
    avg_pace_sec_per_mi = Column(Float)
    is_treadmill = Column(Boolean, default=False)
    temp_f = Column(Float, nullable=True)
    weather_condition = Column(String, nullable=True)
    heat_index_f = Column(Float, nullable=True)
    wet_bulb_f = Column(Float, nullable=True)
    suggested_type = Column(String, default="Easy")
    type_override = Column(String, nullable=True)
    rpe = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    splits_json = Column(Text, default="[]")      # JSON string: list of per-mile splits
    intervals_json = Column(Text, default="[]")   # JSON string: list of raw interval reps
    recovery_json = Column(Text, default="[]")    # JSON string: per-rep [{repIndex,peakHR,recoverySec}], Strava-only
    route_json = Column(Text, default="[]")       # JSON string: list of [lat, lon] GPS points
    route_metrics_json = Column(Text, default="[]")  # JSON string: decimated [{lat,lon,paceSecPerMi,hr,cadence}]
    route_source = Column(String, nullable=True)  # Garmin-only diagnostic: "fit_record_stream" | "geopolyline_summary" | "none"; NULL for Strava rows and pre-rework Garmin rows
    detail_synced_at = Column(String, nullable=True)  # dedup marker — see run_needs_detail_sync()
    exercise_sets_json = Column(Text, nullable=True)  # Garmin-only, strength_training activities: ordered
                                                        # JSON list of {exercise, category, setType, reps,
                                                        # weightLb, durationSec}, see garmin_sync._fetch_exercise_sets.
                                                        # NULL for every other activity type/source — the app's
                                                        # exercise auto-detection is unreliable (often "UNKNOWN"
                                                        # with <50% confidence) until manually corrected in the
                                                        # Garmin Connect app, so this is best-effort, not authoritative

    # Running dynamics, Garmin-only, parsed from the raw .FIT file's session message
    # (not available via Garmin Connect's regular summary API) — see garmin_sync.py
    vertical_oscillation_mm = Column(Float, nullable=True)
    ground_contact_time_ms = Column(Float, nullable=True)
    vertical_ratio_pct = Column(Float, nullable=True)
    stride_length_m = Column(Float, nullable=True)
    avg_power_watts = Column(Float, nullable=True)

    # Phase 6.1 — computed once at sync time (see util.compute_tss/
    # compute_efficiency_factor), same "stored, not recomputed on read" discipline
    # as GAP/run-type elsewhere. tss: hrTSS when avg_hr + a real threshold_hr exist,
    # else a fallback using suggested_type's fixed intensity-factor bucket.
    tss = Column(Float, nullable=True)
    efficiency_factor = Column(Float, nullable=True)

    # Phase 6.3 — gear lifecycle tracking. Auto-assigned at sync time to the
    # user's default gear for this activity's kind (see stats.assign_default_gear),
    # only when unset — never overwrites a manual reassignment. No FK constraint:
    # SQLite doesn't enforce them by default in this app, and gear_summary()
    # already tolerates a dangling gear_id (a deleted gear item) by simply not
    # matching it to anything, rather than needing a migration to null it out.
    gear_id = Column(String, nullable=True)

    # Phase 23 — set on a source="hevy" row once garmin_enrich.process_pending_
    # enrichments has successfully replaced its matched Garmin activity with an
    # enriched (spliced) upload. Doubles as both the "don't re-process this
    # workout on the next auto-sync pass" marker and the pointer needed for a
    # manual revert later (which Garmin activity to delete-and-restore).
    garmin_enriched_activity_id = Column(Integer, nullable=True)


class DailySteps(Base):
    """Garmin-only daily step count. Unlike Run rows (one per activity), this is one row
    per calendar date — a passive wellness metric, not tied to a synced activity.
    Composite (date, user_id) primary key (Phase 1.1) — two real users syncing steps
    for the same calendar date no longer collide. Migrated via a copy-table swap in
    _migrate_daily_steps_composite_pk() below, since SQLite can't ALTER a primary key;
    every pre-existing row's NULL user_id is backfilled to DEFAULT_USER_ID during that
    migration, since a composite PK can't contain NULL."""
    __tablename__ = "daily_steps"

    date = Column(String, primary_key=True)  # YYYY-MM-DD
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, default=DEFAULT_USER_ID)
    steps = Column(Integer)

    # Wellness metrics (Garmin-only, one row per day) — resting_hr_bpm/vo2max come from
    # get_stats(), sleep_* from get_sleep_data(). Each column degrades to NULL
    # independently on a miss rather than blocking the others — see
    # garmin_sync._sync_daily_wellness(). Deliberately scoped to just what was asked
    # for (resting HR, VO2max, sleep) rather than the full 9-metric wellness set from
    # the original plan — every extra metric is another live API call per day, and this
    # account's rate-limit sensitivity this session argued for keeping that cost down.
    resting_hr_bpm = Column(Integer, nullable=True)
    vo2max = Column(Float, nullable=True)
    hrv_last_night_avg_ms = Column(Integer, nullable=True)  # Phase 4.1 — overnight HRV, get_hrv_data()
    hrv_status = Column(String, nullable=True)  # Garmin's own label verbatim, e.g. "BALANCED"
    sleep_score = Column(Integer, nullable=True)
    sleep_seconds = Column(Integer, nullable=True)
    deep_sleep_seconds = Column(Integer, nullable=True)
    light_sleep_seconds = Column(Integer, nullable=True)
    rem_sleep_seconds = Column(Integer, nullable=True)
    awake_sleep_seconds = Column(Integer, nullable=True)
    # Per-night stage timeline (JSON list of {start, end, stage}), for a real hypnogram
    # instead of just daily totals — see garmin_sync._extract_sleep_stages(). Confirmed
    # against real data: Garmin's sleepLevels activityLevel is 0=deep/1=light/2=rem/3=awake.
    sleep_stages_json = Column(Text, default="[]")

    # Phase 24 — daily respiration (get_respiration_data()), waking/sleep averages
    # rather than noisy per-workout FIT session values (which are heavily
    # confounded by workout intensity itself) — an elevated *resting* baseline is
    # the actual illness/fatigue signal, same "resting X trending up" pattern as
    # resting_hr_bpm above. Shape confirmed directly against the live API (unlike
    # get_hrv_data/get_max_metrics above, no defensive multi-path guessing needed).
    avg_waking_respiration_rate = Column(Float, nullable=True)
    avg_sleep_respiration_rate = Column(Float, nullable=True)
    lowest_respiration_rate = Column(Float, nullable=True)
    highest_respiration_rate = Column(Float, nullable=True)

    # Phase 24 — Garmin's OWN acute/chronic training load + ACWR (get_training_status()),
    # a genuine cross-check against this app's own independently-computed CTL/ATL/TSB
    # (Phase 6.2's PMC pipeline) rather than a replacement for it. Undocumented in the
    # sense that garminconnect has no typed method/field names for this specific data
    # (plain nested JSON keyed by device id under mostRecentTrainingStatus), but it's a
    # normal REST JSON response, not raw FIT byte-guessing — much lower risk than the
    # undocumented FIT session fields (Recovery HR/Sweat Loss/Body Battery) elsewhere in
    # this codebase, since there's no encoding/scale ambiguity to get wrong.
    garmin_training_load_acute = Column(Float, nullable=True)
    garmin_training_load_chronic = Column(Float, nullable=True)
    garmin_acwr = Column(Float, nullable=True)  # dailyAcuteChronicWorkloadRatio
    garmin_acwr_status = Column(String, nullable=True)  # Garmin's own label verbatim, e.g. "OPTIMAL"
    garmin_training_status_phrase = Column(String, nullable=True)  # e.g. "MAINTAINING_2"

    wellness_synced_at = Column(String, nullable=True)  # dedup marker, mirrors Run.detail_synced_at


class OAuthToken(Base):
    """Superseded by ProviderCredential (user-scoped) — kept only so the startup
    migration in init_db() has a source row to copy from on upgrade. Not written to
    by new code; safe to drop once every deployment has migrated."""
    __tablename__ = "oauth_tokens"

    provider = Column(String, primary_key=True)  # "strava"
    access_token = Column(String)
    refresh_token = Column(String)
    expires_at = Column(Integer)  # unix timestamp


class SyncMeta(Base):
    __tablename__ = "sync_meta"

    key = Column(String, primary_key=True)
    value = Column(String)


class ChatMessage(Base):
    """Human-visible transcript for the AI chat assistant (see assistant.py). Decoupled
    from the SDK's own live conversation session — this survives container restarts,
    the SDK session's turn-by-turn context does not (an accepted tradeoff, same spirit
    as the in-memory-only backlog-sync job state in main.py)."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by()
    role = Column(String)  # "user" | "assistant"
    content = Column(Text)
    tool_calls_json = Column(Text, nullable=True)  # which real tools/queries backed an assistant reply
    charts_json = Column(Text, nullable=True)  # structured chart specs from render_chart tool calls, or NULL
    created_at = Column(String)  # ISO timestamp
    is_test = Column(Boolean, default=False)  # Phase 12.1 — set when the request carried the
                                                # X-Hale-Test header (verification traffic against a
                                                # real deployment, never real browser usage). Filtered
                                                # out of chat_history, the self-review job, and — more
                                                # importantly — any HealthNote/Workout a test session's
                                                # tool calls create (see coach.create_workout/log_health_note)


class User(Base):
    """Single self-hosted deployment can host multiple users (e.g. a couple/family each
    training toward their own goals with their own Strava/Garmin). No login/session
    enforcement exists yet (password_hash is unused for now) — every request currently
    acts as DEFAULT_USER_ID. This table exists so the data model and sync pipeline are
    genuinely multi-tenant-safe already, not retrofitted after more features are built
    on a single-tenant assumption."""
    __tablename__ = "users"

    id = Column(String, primary_key=True)  # DEFAULT_USER_ID, f"user_{uuid.uuid4().hex[:12]}", or f"demo_{...}"
    email = Column(String, nullable=True, unique=True)
    password_hash = Column(String, nullable=True)
    oidc_subject = Column(String, nullable=True, unique=True)  # OIDC `sub` claim (Phase 1.3), one IdP per user for now
    coach_personality = Column(String, default="normal")  # "encouraging"|"normal"|"spicy"|"insulting"
    is_demo = Column(Boolean, default=False)  # Phase 11 — ephemeral demo login (see app/demo.py)
    expires_at = Column(String, nullable=True)  # ISO timestamp; demo users only, swept by demo.sweep_expired_demo_users()
    created_at = Column(String)
    timezone = Column(String, nullable=True)  # Phase 12.2 — IANA name (e.g. "America/New_York"),
                                                # auto-detected browser-side and PATCHed up once via
                                                # /api/config; NULL means "fall back to the global
                                                # APP_TIMEZONE env var," see util.local_today()
    height_in = Column(Float, nullable=True)  # inches — matches this app's imperial-first convention
                                                # (miles, mph, lb, °F elsewhere). Prerequisite for Phase
                                                # 9.5's planned BMR estimate (LEA readiness flag), which
                                                # also needs weight_lb/date_of_birth/sex below — all four
                                                # added together rather than piecemeal, editable via
                                                # GET/PATCH /api/profile (see routes/settings.py)
    weight_lb = Column(Float, nullable=True)
    date_of_birth = Column(String, nullable=True)  # "YYYY-MM-DD", same string-date convention as Run.date
    sex = Column(String, nullable=True)  # "male"|"female"|"other" — Mifflin-St Jeor (Phase 9.5's likely
                                           # BMR formula) only has male/female coefficients; how "other"
                                           # should be handled there is an open question for whoever
                                           # implements 9.5, not resolved here


class ProviderCredential(Base):
    """Per-user third-party connection (Strava OAuth tokens, Garmin username/password,
    future Google Health/Withings/...). Replaces both the old global oauth_tokens row
    and the GARMIN_EMAIL/GARMIN_PASSWORD env vars as the source of truth — env vars are
    now only used to seed the default user's row on first boot (see init_db()).
    Password is plaintext today, same exposure GARMIN_PASSWORD already has as an env
    var — at-rest encryption is a distinct, separately-tracked future requirement, not
    solved here."""
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_credentials_user_provider"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"))
    provider = Column(String)  # "strava" | "garmin" | "google_health" | "withings" | ...
    access_token = Column(String, nullable=True)   # Strava OAuth
    refresh_token = Column(String, nullable=True)
    expires_at = Column(Integer, nullable=True)
    username = Column(String, nullable=True)        # Garmin
    password = Column(String, nullable=True)        # Garmin
    created_at = Column(String)


class ApiToken(Base):
    """Device/headless-client auth tokens (Phase 1.5's token management UI, Phase 3's
    Android client). Issued once, shown to the user exactly once at creation time —
    only a SHA-256 hash is ever persisted, never the raw token, so a DB leak alone
    can't be used to authenticate as the user. A whole new table, so create_all()
    picks it up automatically — no _MIGRATABLE_TABLES entry needed for its first
    version, same as ProviderCredential/HealthNote/Workout before it."""
    __tablename__ = "api_tokens"

    id = Column(String, primary_key=True)  # f"tok_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String, nullable=False, unique=True)  # sha256 hex digest of the raw token
    name = Column(String, nullable=True)  # user-chosen label, e.g. "Pixel 9 Pro"
    created_at = Column(String)
    last_used_at = Column(String, nullable=True)


class PushSubscription(Base):
    """Web Push subscription (Phase 0.11). One row per browser/device the user has
    granted notification permission on — a user with two devices has two rows, both
    valid targets for push.send_push(). A whole new table, so create_all() picks it
    up automatically, same as ApiToken above. Unlike Run/Goal there's no pre-multi-
    tenant legacy-NULL user_id to handle for a table this new, so plain equality
    filtering (not owned_by()) is correct here — matches ApiToken's own convention."""
    __tablename__ = "push_subscriptions"

    id = Column(String, primary_key=True)  # f"push_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    endpoint = Column(String, nullable=False, unique=True)  # push service URL, unique per browser subscription
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    created_at = Column(String)


class Goal(Base):
    """A user's training goal — one wide table covers all three types (matches this
    app's existing convention, e.g. Run's nullable Garmin-only columns, rather than a
    table per type). See stats.goal_progress() for how each type's progress is computed."""
    __tablename__ = "goals"

    id = Column(String, primary_key=True)  # f"goal_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by()
    goal_type = Column(String)             # "race" | "consistency" | "distance_target"
    name = Column(String)
    status = Column(String, default="active")  # "active" | "completed" | "abandoned"
    activity_types_json = Column(Text, default='["Run"]')  # e.g. ["Run","Ride"] for a duathlon

    target_value = Column(Float, nullable=True)   # race: distance mi; consistency: N; distance_target: cumulative mi
    target_unit = Column(String, nullable=True)   # "miles" | "runs_per_week" | "miles_per_week"
    target_date = Column(String, nullable=True)   # race: event date; distance_target: optional deadline; YYYY-MM-DD
    start_date = Column(String, nullable=True)    # distance_target only: window start, defaults to created_at's date

    notes = Column(Text, nullable=True)
    created_at = Column(String)
    completed_at = Column(String, nullable=True)
    linked_run_id = Column(String, nullable=True)  # race goals: the actual Run matched to this event, see stats._find_matching_race_run
    priority = Column(Integer, default=0)  # lower shows first; legacy-NULL rows (pre-dating this
                                             # column) treated as 0 at read/sort time, same pattern
                                             # as everywhere else in this codebase


class Gear(Base):
    """Shoe/bike/bike-component lifecycle tracking (Phase 6.3). Mileage is always
    computed at read time from linked Run rows (stats.gear_summary), never stored/
    recomputed at sync time — unlike TSS/GAP elsewhere, gear assignment can change
    retroactively (reassigning a batch of old runs to a different pair), so a
    stored running total would need its own invalidation logic for no real benefit.
    `kind="bike_component"` rows track a sub-part of a bike (chain, tires) via
    `parent_gear_id` — their mileage is the parent bike's ride mileage from their
    own `start_date` onward, not directly linked to any Run themselves (see
    stats.gear_summary), so replacing a chain resets only its own counter."""
    __tablename__ = "gear"

    id = Column(String, primary_key=True)  # f"gear_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by()
    name = Column(String)
    kind = Column(String)  # "shoe" | "bike" | "bike_component"
    parent_gear_id = Column(String, nullable=True)  # bike_component only -> its bike's Gear.id
    is_default = Column(Boolean, default=False)  # auto-assigned to new runs of the matching kind at sync time; at most one per (user, kind)
    start_date = Column(String, nullable=True)  # YYYY-MM-DD
    retired_date = Column(String, nullable=True)
    replace_at_mi = Column(Float, nullable=True)  # optional wear-alert threshold
    notes = Column(Text, nullable=True)
    created_at = Column(String)


class HealthNote(Base):
    """Deliberately broader than "injury" — covers anything the athlete reports that
    should affect training: musculoskeletal injuries, illness, chronic-condition
    flare-ups, scheduled medical procedures, and purely temporary things (a migraine, a
    hangover) with no diagnosis at all. `category` is what distinguishes these; only
    category == "injury" populates body_area and gets automatic recurrence linking
    (see coach.find_related_health_history) since only that category has a reliable
    "same spot" match key — every other category is still fully tracked (logged,
    checked in on, never deleted) but relies on the coach noticing relevant history via
    the get_health_history tool rather than a DB-level auto-link. Rows are never
    deleted, only transitioned through status — matches this app's established
    retain-raw-data philosophy (see HR-glitch/duplicate-run handling elsewhere)."""
    __tablename__ = "health_notes"

    id = Column(String, primary_key=True)  # f"health_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by()
    category = Column(String)  # "injury"|"illness"|"chronic_flare"|"procedure"|"other"
    body_area = Column(String, nullable=True)  # only meaningful when category == "injury"
    suspected_type = Column(Text, nullable=True)  # free text, LLM-authored: "COVID", "migraine", "broken finger"
    suspected_severity = Column(String, nullable=True)  # "mild"|"moderate"|"severe" — optional, not every
                                                          # category needs this framing (a hangover doesn't)
    training_impact = Column(Text, nullable=True)  # free text: what's actually restricted vs fine — e.g. a
                                                     # broken finger: "avoid grip/strength work, running is fine"
    date_reported = Column(String)  # YYYY-MM-DD
    expected_clear_date = Column(String, nullable=True)
    status = Column(String, default="active")  # "active"|"monitoring"|"resolved"
    last_check_in_at = Column(String, nullable=True)  # ISO timestamp — rate-limits the check-in question to 1/day
    resolved_at = Column(String, nullable=True)
    related_note_id = Column(String, nullable=True)  # plain self-ref, mirrors Goal.linked_run_id; only ever
                                                       # set for category == "injury"
    notes = Column(Text, nullable=True)
    created_at = Column(String)
    is_test = Column(Boolean, default=False)  # Phase 12.1 — see ChatMessage.is_test's comment;
                                                # excluded from list_health_notes and therefore from
                                                # every real chat session's context


class Workout(Base):
    """A coach-scheduled or manually-created prescribed session. Deliberately separate
    from Goal (a target/status entity tracked via stats.goal_progress()) — a Workout is
    a specific session on a specific date that can be auto-linked to the real Run that
    satisfies it, see coach._find_and_link_workout_run()."""
    __tablename__ = "workouts"

    id = Column(String, primary_key=True)  # f"workout_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    scheduled_date = Column(String)  # YYYY-MM-DD
    workout_type = Column(String)  # "easy"|"tempo"|"interval"|"long"|"rest"|"strength"|"cross_train"
    activity_type = Column(String, default="Run")  # "Run"|"Ride"|"Walk"|... — mirrors Run.activity_type,
                                                     # deliberately separate from workout_type: workout_type is
                                                     # training-intensity flavor, activity_type is what it can
                                                     # auto-link against — a "tempo" workout is still a Run,
                                                     # never a Ride
    target_distance_mi = Column(Float, nullable=True)
    target_pace_sec_per_mi = Column(Integer, nullable=True)
    target_duration_sec = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)  # coach's prescription rationale
    steps_json = Column(Text, nullable=True)  # ordered JSON list of structured steps, see
                                                # coach.VALID_STEP_SIDES / coach._steps_to_dicts —
                                                # nullable/legacy-NULL for workouts scheduled before
                                                # this column existed or for simple single-block
                                                # sessions (e.g. a plain easy run) that don't need
                                                # step-by-step breakdown; notes stays the free-text
                                                # rationale either way
    status = Column(String, default="planned")  # "planned"|"completed"|"skipped"|"modified"
    linked_run_id = Column(String, nullable=True)  # set once matched/critiqued, mirrors Goal.linked_run_id
    critique_text = Column(Text, nullable=True)
    created_at = Column(String)
    source = Column(String, default="coach")  # "coach"|"garmin"|"generator" — each source's rows never
                                                 # overwrite another source's row for the same date (and
                                                 # vice versa); legacy-NULL rows (pre-dating this column) are
                                                 # treated as "coach" at read time, same pattern as
                                                 # owned_by()'s NULL-user_id handling
    garmin_workout_uuid = Column(String, nullable=True)  # only set when source="garmin" — Garmin's own
                                                            # identifier for this specific suggestion, so a
                                                            # resync can detect Garmin silently swapping the
                                                            # suggested workout for a date (see
                                                            # garmin_sync._fetch_adaptive_plan_workouts)
    scheduled_time = Column(String, nullable=True)  # Phase 4.3 — "HH:MM", only set for the 2nd session on a
                                                       # generator-scheduled two-a-day; a single daily session
                                                       # has no need to disambiguate ordering, so stays NULL
    is_test = Column(Boolean, default=False)  # Phase 12.1 — see ChatMessage.is_test's comment;
                                                # excluded from list_workouts and therefore from every
                                                # real chat session's context


class UserTrainingConfig(Base):
    """Per-user training parameters the generator (Phase 4.3) and endurance step
    targets (Phase 4.2) read from — one flat row per user, distinct from
    ExerciseProgress (Phase 4.4), which tracks per-exercise progression state rather
    than these fixed configuration values."""
    __tablename__ = "user_training_config"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    max_hr = Column(Integer, nullable=True)
    threshold_hr = Column(Integer, nullable=True)
    ftp_watts = Column(Integer, nullable=True)
    zones_json = Column(Text, nullable=True)  # 5-zone HR bounds; null = derive from max_hr (208 - 0.7*age default)
    weekly_ramp_pct = Column(Float, default=3.0)
    mesocycle_pattern = Column(String, default="3:1")
    distribution = Column(String, default="pyramidal")
    strength_days_per_week = Column(Integer, default=2)
    strength_template = Column(String, default="full_body_ab")  # selects the rotation in generator.py


class ExerciseProgress(Base):
    """Phase 4.4 — per-exercise progression state for the strength generator's double-
    progression rule (see generator.py). Separate from UserTrainingConfig (one flat
    row per user) since each exercise in the rotation progresses independently."""
    __tablename__ = "exercise_progress"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    exercise = Column(String, primary_key=True)  # matches the template's exercise name exactly
    current_weight_lb = Column(Float, nullable=True)  # null = bodyweight
    current_reps_target = Column(Integer, default=8)
    current_hold_sec = Column(Integer, nullable=True)  # only set for isometric exercises
    last_completed_at = Column(String, nullable=True)


class WeeklyPlan(Base):
    """Phase 4.3 — one row per (user, week). `target_tss`/`actual_tss` are named for
    the spec's eventual real Training Stress Score (Phase 6.1's per-activity TSS
    hasn't shipped yet) but store a mileage-based proxy for now — same "real number
    now, real TSS once Phase 6 exists" tradeoff stats.readiness() already makes for
    acuteChronicRatio. `frozen` marks a week whose budget didn't ramp because a
    2+-flag readiness day capped it that week; the next week ramps from the frozen
    base, not from the (unmet) target, so a bad week doesn't get "made up" all at once."""
    __tablename__ = "weekly_plan"
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_weekly_plan_user_week"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    week_start = Column(String, nullable=False)  # YYYY-MM-DD, Monday
    target_tss = Column(Float, nullable=True)  # mileage proxy until Phase 6
    actual_tss = Column(Float, nullable=True)
    is_deload = Column(Boolean, default=False)
    frozen = Column(Boolean, default=False)


class DailyMetrics(Base):
    """Phase 6.2 — one row per (user, date), the PMC (fitness/fatigue/form) pipeline's
    output. `daily_load` is that day's total training stress (sum of Run.tss across
    every activity on that date — Phase 6.1's per-run TSS/hrTSS, not a separate
    formula). `ctl`/`atl` are the standard 42-day/7-day exponentially-weighted
    rolling averages of daily_load ("fitness"/"fatigue"); `tsb` ("form") is the
    *previous* day's ctl minus atl — the standard TrainingPeaks convention of
    representing freshness at the start of a day, before that day's own training is
    factored in. Always fully recomputed from Run history (app/pipeline.py), never
    incrementally adjusted — same discipline as Phase 6.1's backfill_run_metrics.
    Deliberately narrower than the field list floated while first scoping this
    phase: no `readiness_score` (this codebase's established principle is never to
    fabricate a composite score — see stats.readiness()/goal_progress()'s own
    docstrings) and no `hrv_baseline_ms`/`time_in_zone_json` (already computed
    on-the-fly elsewhere; storing a second copy here isn't needed for the PMC
    chart this phase actually exists to ship)."""
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_daily_metrics_user_date"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date = Column(String, nullable=False)  # YYYY-MM-DD
    daily_load = Column(Float, nullable=False, default=0.0)
    ctl = Column(Float, nullable=False)
    atl = Column(Float, nullable=False)
    tsb = Column(Float, nullable=False)
    computed_at = Column(String, nullable=True)


class CoachIssueDraft(Base):
    """Phase 12.5 — one rolling draft GitHub issue per user, accumulated from two
    sources: the periodic self_review job (scans real, non-test ChatMessage history
    for coach bugs/gaps like date confusion) and the live log_product_feedback chat
    tool (a message classified as a bug report/feature request/product feedback about
    HALE itself, not a coaching question, gets appended immediately rather than
    waiting for the next scheduled review). Both append a new dated markdown section
    via coach.self_review.append_to_draft rather than overwriting — deliberately
    never auto-posted to github.com (see CLAUDE.md); fetched via GET /api/coach-issue,
    downloaded as .md from Settings, and cleared (this row deleted) once pulled, at
    which point the next finding starts a fresh draft."""
    __tablename__ = "coach_issue_drafts"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    title = Column(String, nullable=True)
    body_markdown = Column(Text, nullable=True)
    frustration_count = Column(Integer, default=0)
    updated_at = Column(String, nullable=True)
    last_reviewed_chat_message_id = Column(Integer, nullable=True)  # checkpoint into
                                                                       # ChatMessage.id so
                                                                       # the periodic job
                                                                       # never reprocesses
                                                                       # the same messages


class RecoveryTool(Base):
    """A recovery device the athlete owns that the coach can factor into
    recommendations — e.g. compression boots. Deliberately concrete/narrow for now
    (seeded below for this account's actual device); a user-facing way to add new
    tools via conversation is a known follow-up, not built yet (see STATUS.md) — this
    table's shape (min/max ranges rather than hardcoded numbers baked into the coach's
    prompt) is what makes that later step additive instead of a schema rework."""
    __tablename__ = "recovery_tools"

    id = Column(String, primary_key=True)  # f"recoverytool_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # see owned_by()
    name = Column(String)  # "Hyperice Normatec Elite"
    category = Column(String)  # "compression_boots" — controlled vocab, coach.VALID_RECOVERY_CATEGORIES
    min_level = Column(Integer, default=1)
    max_level = Column(Integer, default=7)
    min_duration_min = Column(Integer, default=15)
    max_duration_min = Column(Integer, default=60)
    duration_increment_min = Column(Integer, default=15)
    supports_zone_boost = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(String)


class RecoverySession(Base):
    """A specific recovery session using a RecoveryTool, coach-recommended (see
    coach.recommend_recovery_session). Mirrors Workout's planned/completed/skipped
    lifecycle but kept as its own table rather than folded into Workout: a recovery
    session isn't a trackable Strava/Garmin activity_type and has no auto-link target,
    so Workout's activity_type/target_distance_mi/target_pace_sec_per_mi fields don't
    apply here at all."""
    __tablename__ = "recovery_sessions"

    id = Column(String, primary_key=True)  # f"recovery_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    tool_id = Column(String)  # plain ref to RecoveryTool.id — unenforced FK, same pattern as Goal.linked_run_id
    scheduled_date = Column(String)  # YYYY-MM-DD
    level = Column(Integer)
    duration_min = Column(Integer)
    zone_boost = Column(Boolean, default=False)
    rationale = Column(Text, nullable=True)  # coach's reasoning for these specific settings that day
    status = Column(String, default="planned")  # "planned"|"completed"|"skipped"
    created_at = Column(String)


def get_sync_meta(key: str):
    db = SessionLocal()
    try:
        row = db.get(SyncMeta, key)
        return row.value if row else None
    finally:
        db.close()


def set_sync_meta(key: str, value: str):
    db = SessionLocal()
    try:
        row = db.get(SyncMeta, key) or SyncMeta(key=key)
        row.value = value
        db.merge(row)
        db.commit()
    finally:
        db.close()


# Per-user keys prefixed with "u:{user_id}:{key}" (Phase 1.4) — genuinely user-specific
# state (sync timestamps/counts/errors, dashboard cache, Garmin cooldown/backlog cursor)
# gets namespaced through this so two real users never clobber each other's sync_meta
# rows. Deliberately NOT applied to the geocode cache (`f"geocode_{lat:.2f}_{lon:.2f}"`)
# — that's keyed by physical location, not by who's asking, and should stay a single
# shared cache across every user.
def user_key(user_id: str, key: str) -> str:
    return f"u:{user_id}:{key}"


_LEGACY_GLOBAL_SYNC_META_KEYS = [
    "strava_last_synced_at", "strava_last_count", "strava_last_error",
    "garmin_last_synced_at", "garmin_last_count", "garmin_last_error",
    "strava_backlog_last_synced_at", "strava_backlog_last_count", "strava_backlog_last_error",
    "garmin_backlog_last_synced_at", "garmin_backlog_last_count", "garmin_backlog_last_error",
    "dashboard_summary_cache", "dashboard_summary_cache_updated_at",
    "garmin_adaptive_plan_last_checked_at", "garmin_rate_limit_cooldown_until",
    "garmin_rate_limit_consecutive_failures", "garmin_activities_backlog_offset",
    "garmin_activities_backlog_complete",
]


def _migrate_sync_meta_to_user_keys():
    """One-time copy of every pre-Phase-1.4 global sync_meta key to its DEFAULT_USER_ID-
    namespaced equivalent, so upgrading doesn't silently lose sync history, Garmin
    cooldown state, or the backlog cursor (all previously global, now per-user).
    Copies rather than moves — the old global key is left in place, untouched, so a
    rollback to a pre-1.4 build still reads its own expected keys. Idempotent: only
    copies a key if its namespaced target doesn't already have a value."""
    for key in _LEGACY_GLOBAL_SYNC_META_KEYS:
        old_value = get_sync_meta(key)
        if old_value is None:
            continue
        new_key = user_key(DEFAULT_USER_ID, key)
        if get_sync_meta(new_key) is None:
            set_sync_meta(new_key, old_value)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_add_missing_columns()
    _migrate_daily_steps_composite_pk()
    _migrate_sync_meta_to_user_keys()
    _backfill_detail_synced_at()
    _seed_default_user_and_credentials()
    _seed_marathon_goal()
    _seed_default_recovery_tool()


# Tables that have grown columns since their first release and need the ALTER TABLE
# patch below. Whole NEW tables (ProviderCredential) don't need this — create_all()
# already creates them from scratch with every current column. User/Workout WERE
# whole-new-table exceptions too, but have each grown a column since (coach_personality;
# scheduled_time/source) so they have to be here now like any other existing table.
# HealthNote was the same kind of exception until Phase 12.1's is_test column — it was
# missing from this list even though it should have been added the moment Workout was
# (a stale gap, not a deliberate choice), so its own real production table would
# otherwise never have gained is_test via ALTER TABLE.
_MIGRATABLE_TABLES = [("runs", Run), ("daily_steps", DailySteps), ("chat_messages", ChatMessage),
                       ("goals", Goal), ("users", User), ("workouts", Workout),
                       ("health_notes", HealthNote)]


def _migrate_add_missing_columns():
    """SQLAlchemy's create_all() only creates tables that don't exist yet — it
    won't add new columns to a table that's already there. Since this project
    has no migration framework, patch that gap with a plain ALTER TABLE for
    any model column SQLite doesn't have yet. No DB-level DEFAULT is set here —
    matches this repo's established pattern of backfilling at read time instead
    (e.g. `r.recovery_json or "[]"`; for user_id specifically, see owned_by())."""
    with engine.connect() as conn:
        for table_name, model in _MIGRATABLE_TABLES:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
            for col in model.__table__.columns:
                if col.name not in existing:
                    col_type = "TEXT" if isinstance(col.type, (String, Text)) else \
                        "INTEGER" if isinstance(col.type, (Integer, Boolean)) else "REAL"
                    conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}")
        conn.commit()


def _migrate_daily_steps_composite_pk():
    """SQLite can't ALTER a primary key, so upgrading daily_steps from its original
    plain `date` PK to a composite (date, user_id) PK (Phase 1.1) requires a full
    copy-table swap: build a new table with the target schema (reflected from the
    live table via PRAGMA table_info, so it carries every column — including any
    added since by _migrate_add_missing_columns(), which must run before this),
    copy every row across while backfilling NULL user_id to DEFAULT_USER_ID (a
    composite PK can't contain NULL), drop the old table, rename the new one into
    place. Idempotent: checks whether user_id is already part of the primary key
    and no-ops if so — true for both an already-migrated existing DB and a brand
    new one (create_all() above already builds the composite-PK schema from
    scratch for a fresh install, so this has nothing to do there either)."""
    with engine.connect() as conn:
        info = conn.exec_driver_sql("PRAGMA table_info(daily_steps)").fetchall()
        pk_cols = {row[1] for row in info if row[5] > 0}
        if "user_id" in pk_cols:
            return

        col_names = [row[1] for row in info]
        col_ddl = ", ".join(f'"{row[1]}" {row[2]}' for row in info)
        select_cols = ", ".join(
            f"COALESCE(\"user_id\", '{DEFAULT_USER_ID}')" if name == "user_id" else f'"{name}"'
            for name in col_names
        )
        insert_cols = ", ".join(f'"{c}"' for c in col_names)

        conn.exec_driver_sql(f'CREATE TABLE daily_steps_new ({col_ddl}, PRIMARY KEY (date, user_id))')
        conn.exec_driver_sql(f"INSERT INTO daily_steps_new ({insert_cols}) SELECT {select_cols} FROM daily_steps")
        conn.exec_driver_sql("DROP TABLE daily_steps")
        conn.exec_driver_sql("ALTER TABLE daily_steps_new RENAME TO daily_steps")
        conn.commit()


def _backfill_detail_synced_at():
    """One-time, idempotent: a Run row only ever gets db.merge()'d at the very end of
    a fully-successful _process_activity() — every earlier failure path (including
    Garmin's rate-limit re-raise) exits before that point. So "a Run row exists"
    already implies "it was fully detail-synced"; this just makes that explicit for
    every row that predates the detail_synced_at column. Without this, the first sync
    after upgrading would treat the entire existing history as unsynced and try to
    re-fetch everything — the exact rate-limit wall this column exists to avoid."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "UPDATE runs SET detail_synced_at = ? WHERE detail_synced_at IS NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()


def run_needs_detail_sync(db, run_id: str) -> bool:
    """Shared skip-check used by both strava.py and garmin_sync.py's sync loops —
    called at the loop level (not inside _process_activity) so a skip costs nothing:
    no API call, no retry-wrapper sleep."""
    existing = db.get(Run, run_id)
    return existing is None or not existing.detail_synced_at


def resolve_run_id(db, source: str, activity_id, user_id: str) -> str:
    """Returns the Run.id to use for a given (source, raw activity id, user) — shared
    by both strava.py and garmin_sync.py (their loop-level dedup check AND their
    _process_activity, so both always agree on the same id for the same activity).
    The plain f"{source}_{activity_id}" id is used in the common case (no existing
    row, or an existing row that's unowned or already belongs to this same user);
    only falls back to a user-suffixed id on a genuine cross-user conflict — two
    different real users' Strava/Garmin accounts happening to produce the same raw
    numeric activity id, which would otherwise silently reassign the first user's
    Run row to the second user (Run.id is a single-column PK with no user_id
    component)."""
    plain_id = f"{source}_{activity_id}"
    existing = db.get(Run, plain_id)
    if existing is None or existing.user_id in (None, user_id):
        return plain_id
    return f"{source}_{user_id}_{activity_id}"


def day_needs_wellness_sync(db, date_str: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Same dedup principle as run_needs_detail_sync, applied to DailySteps' wellness
    columns (resting HR/VO2max/sleep) instead of activities — a settled day's row
    won't be re-fetched. Callers are responsible for the trailing "volatile window"
    exception (today's/yesterday's data can still change), same as _sync_daily_steps."""
    existing = db.get(DailySteps, (date_str, user_id))
    return existing is None or not existing.wellness_synced_at


def _seed_default_user_and_credentials():
    """One-time, idempotent: ensures a DEFAULT_USER_ID user exists, and that its
    provider credentials are populated from whatever this deployment already had
    (the old single-row oauth_tokens table for Strava, GARMIN_EMAIL/GARMIN_PASSWORD
    env vars for Garmin) — so upgrading to the multi-user schema is invisible to an
    existing single-user deployment. Never overwrites a credential that's already
    been migrated or entered through the new Connections UI."""
    db = SessionLocal()
    try:
        if db.get(User, DEFAULT_USER_ID) is None:
            db.add(User(id=DEFAULT_USER_ID, created_at=datetime.now(timezone.utc).isoformat()))
            db.commit()

        have = {c.provider for c in db.query(ProviderCredential).filter(ProviderCredential.user_id == DEFAULT_USER_ID)}

        if "strava" not in have:
            old = db.get(OAuthToken, "strava")
            if old:
                db.add(ProviderCredential(
                    user_id=DEFAULT_USER_ID, provider="strava",
                    access_token=old.access_token, refresh_token=old.refresh_token, expires_at=old.expires_at,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))

        if "garmin" not in have:
            email, password = os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD")
            if email and password:
                db.add(ProviderCredential(
                    user_id=DEFAULT_USER_ID, provider="garmin", username=email, password=password,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))

        db.commit()
    finally:
        db.close()


def _seed_marathon_goal():
    """One-time: migrates the app's original hardcoded MARATHON_DATE race countdown
    into a real Goal row, so upgrading doesn't lose it. Only runs when the goals table
    is completely empty — never touches user-created goals."""
    db = SessionLocal()
    try:
        if db.query(Goal).count() == 0:
            db.add(Goal(
                id=f"goal_{uuid.uuid4().hex[:12]}", user_id=DEFAULT_USER_ID, goal_type="race",
                name="Manchester City Marathon", status="active", activity_types_json='["Run"]',
                target_value=26.2, target_unit="miles", target_date="2026-11-08",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            db.commit()
    finally:
        db.close()


def _seed_default_recovery_tool():
    """One-time: seeds this account's real Hyperice Normatec Elite compression boots as
    a RecoveryTool so the coach can recommend sessions on it immediately, without the
    user having to describe it in chat first. Only runs when the table is completely
    empty — never touches anything a user (or the coach, once self-service creation
    ships — see RecoveryTool's docstring) adds later."""
    db = SessionLocal()
    try:
        if db.query(RecoveryTool).count() == 0:
            db.add(RecoveryTool(
                id=f"recoverytool_{uuid.uuid4().hex[:12]}", user_id=DEFAULT_USER_ID,
                name="Hyperice Normatec Elite", category="compression_boots",
                min_level=1, max_level=7, min_duration_min=15, max_duration_min=60,
                duration_increment_min=15, supports_zone_boost=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            db.commit()
    finally:
        db.close()
