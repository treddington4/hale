"""AI chat assistant — answers questions about the user's own stored fitness data by
calling read-only tools backed by stats.py. Uses the Claude Agent SDK, authenticated
via either a Claude Pro/Max subscription token (CLAUDE_CODE_OAUTH_TOKEN, generated
once via `claude setup-token` on any machine, no per-token metered cost) or a standard
metered API key (ANTHROPIC_API_KEY) — whichever the SDK finds first (its own default
precedence, not overridden here). Both are optional; if neither is set, is_configured()
returns False and callers should show a clean "not configured yet" state rather than
attempting a query.

SECURITY: this agent runs unattended, server-side, in the same container as the app
and its SQLite DB file. Nothing about this feature should let the model touch the
container filesystem or shell. Defense in depth, not just one allowlist: `allowed_tools`
lists only our own mcp__runlog__* tools, `disallowed_tools` explicitly blocks
Claude Code's built-in filesystem/shell tools as a second layer, and `setting_sources`
is empty so no local .claude/settings.json in the container can grant anything back.
Write access is narrowly and deliberately scoped: only HealthNote and Workout rows
(via coach.py), plus CoachIssueDraft (via self_review.py, Phase 12.5 — a rolling
draft-only note-to-self, never posted anywhere) can be written by any tool here —
never Run, Goal, ProviderCredential, or anything filesystem/shell-adjacent. Every
stats.py-backed tool remains a plain SELECT.
"""
import json
import os
from datetime import datetime, timezone

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from . import core as coach
from .. import stats
from ..models import SessionLocal, ChatMessage, User, DEFAULT_USER_ID

BUILTIN_TOOLS_BLOCKLIST = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Task", "WebFetch", "WebSearch",
    "Glob", "Grep", "EnterWorktree", "ExitWorktree", "CronCreate", "CronDelete", "CronList",
]

# Names only — fixed regardless of which user's session is asking, so this can stay a
# module-level constant even though the tool objects themselves (_build_tools below)
# are rebuilt per user.
_TOOL_NAMES = [
    "get_run_summary", "get_weekly_mileage", "get_monthly_mileage", "get_personal_records",
    "get_pace_trend", "get_training_load_trend", "get_readiness", "get_daily_steps", "query_runs", "get_run_detail",
    "get_health_history", "find_related_health_history", "log_health_note", "update_health_status",
    "get_scheduled_workouts", "schedule_workout", "update_workout", "record_workout_completion",
    "get_exercise_progress", "log_product_feedback",
    "render_chart", "get_goals",
    "get_recovery_tools", "recommend_recovery_session", "get_recovery_sessions",
    "search_chat_history",
]
ALLOWED_TOOL_NAMES = [f"mcp__runlog__{name}" for name in _TOOL_NAMES]


def _db_call(fn, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    finally:
        db.close()


def _build_tools(user_id: str, is_test: bool = False) -> list:
    """Returns a fresh set of tool closures bound to one user's id, so every DB
    read/write a tool makes is actually scoped to that user — not just the SDK session
    wrapper around them (see _get_client). Called once per (user, is_test), when that
    session's client is first created, not per message. `is_test` (Phase 12.1) is
    stamped onto every row the two write-capable tools below (log_health_note,
    schedule_workout) create, so a verification session's tool calls never pollute a
    real user's health/workout history."""

    @tool("get_run_summary", "Totals/averages (count, distance, pace, elevation, time) over an optional date range. Pace is always in MM:SS/mi format — quote it exactly, never convert to sec/mi.", {
        "type": "object",
        "properties": {
            "startDate": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "endDate": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "activityType": {"type": "string", "description": "e.g. 'Run', 'Ride', 'Walk'. Defaults to 'Run'."},
        },
    })
    async def get_run_summary(args):
        result = _db_call(stats.run_summary, args.get("startDate"), args.get("endDate"),
                           args.get("activityType", "Run"), user_id=user_id)
        activity_type = args.get("activityType", "Run")
        pace_display = stats.format_pace_or_speed(result.get("avgPaceSecPerMi"), activity_type)
        filtered = {k: v for k, v in result.items() if k != "avgPaceSecPerMi"}
        filtered["avgPaceDisplay"] = pace_display or "n/a"
        return {"content": [{"type": "text", "text": json.dumps(filtered)}]}

    @tool("get_weekly_mileage", "Weekly mileage totals for the trailing N weeks", {
        "type": "object",
        "properties": {"weeks": {"type": "integer", "description": "Default 12"}},
    })
    async def get_weekly_mileage(args):
        result = _db_call(stats.weekly_mileage, args.get("weeks", 12), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_monthly_mileage", "Monthly mileage totals for the trailing N months", {
        "type": "object",
        "properties": {"months": {"type": "integer", "description": "Default 12"}},
    })
    async def get_monthly_mileage(args):
        result = _db_call(stats.monthly_mileage, args.get("months", 12), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_personal_records", "Longest run, fastest plausible pace, most elevation, longest duration", {
        "type": "object", "properties": {},
    })
    async def get_personal_records(args):
        result = _db_call(stats.personal_records, user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_pace_trend", "Rolling window pace trend over the trailing N days", {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Default 90"},
            "windowDays": {"type": "integer", "description": "Rolling window size in days, default 7"},
        },
    })
    async def get_pace_trend(args):
        result = _db_call(stats.rolling_pace_trend, args.get("days", 90), args.get("windowDays", 7), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_training_load_trend", "Trailing 4 weeks vs prior 4 weeks mileage, with percent change and direction", {
        "type": "object", "properties": {},
    })
    async def get_training_load_trend(args):
        result = _db_call(stats.training_load_trend, user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_readiness", "Today's (or a given date's) readiness signal — HRV/resting-HR vs 7-day baseline, sleep score, 7d/28d acute:chronic mileage ratio, days since a hard run, and named flags (hrv_below_baseline/rhr_spike/sleep_deficit). This is what the workout generator's readiness gate uses — check it before suggesting anything more intense than easy if the user hasn't mentioned how they're feeling.", {
        "type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
        },
    })
    async def get_readiness(args):
        result = _db_call(stats.readiness, user_id=user_id, date=args.get("date"))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_goals", "The user's active goals with real computed progress (countdown, recent volume, etc — no invented \"on track\" verdict, just the numbers). Returned in priority order — lower priority number is more important. Check this before giving training advice when the user has an active race/consistency/distance goal, so guidance actually serves what they're working toward.", {
        "type": "object", "properties": {},
    })
    async def get_goals(args):
        result = _db_call(stats.list_active_goals_with_progress, user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_daily_steps", "Daily step counts (Garmin-only) for the trailing N days", {
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "Default 30"}},
    })
    async def get_daily_steps(args):
        result = _db_call(stats.daily_steps_summary, args.get("days", 30), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("query_runs", "Flexible run lookup: filter by date range/type/distance, sort, limit. Use for free-form questions the other tools don't cover. Pace is always in MM:SS/mi format — quote exactly, never convert.", {
        "type": "object",
        "properties": {
            "startDate": {"type": "string"},
            "endDate": {"type": "string"},
            "activityType": {"type": "string", "description": "Omit for all activity types"},
            "minDistance": {"type": "number"},
            "maxDistance": {"type": "number"},
            "sortBy": {"type": "string", "description": "'date' (default), 'distance_mi', 'elev_gain_ft', or 'pace' (ascending, fastest first)"},
            "limit": {"type": "integer", "description": "Default 20"},
        },
    })
    async def query_runs(args):
        result = _db_call(
            stats.query_runs,
            args.get("startDate"), args.get("endDate"), args.get("activityType"),
            args.get("minDistance"), args.get("maxDistance"),
            args.get("sortBy", "date"), args.get("limit", 20), user_id=user_id,
        )
        formatted = []
        for r in result:
            r_copy = dict(r)
            pace_display = stats.format_pace_or_speed(r.get("paceSecPerMi"), r.get("activityType", "Run"))
            if "paceSecPerMi" in r_copy:
                del r_copy["paceSecPerMi"]
            r_copy["paceDisplay"] = pace_display or "n/a"
            formatted.append(r_copy)
        return {"content": [{"type": "text", "text": json.dumps(formatted)}]}

    @tool("get_run_detail", "Full detail for a single run by id (splits, HR, notes) — use the id from query_runs/get_personal_records. Pace is in MM:SS/mi — quote exactly.", {
        "type": "object",
        "properties": {"runId": {"type": "string"}},
        "required": ["runId"],
    })
    async def get_run_detail(args):
        result = _db_call(stats.run_detail, args["runId"], user_id=user_id)
        if result is None:
            return {"content": [{"type": "text", "text": f"No run found with id {args['runId']}"}], "is_error": True}
        r_copy = dict(result)
        pace_display = stats.format_pace_or_speed(result.get("paceSecPerMi"), result.get("activityType", "Run"))
        if "paceSecPerMi" in r_copy:
            del r_copy["paceSecPerMi"]
        r_copy["paceDisplay"] = pace_display or "n/a"
        return {"content": [{"type": "text", "text": json.dumps(r_copy)}]}

    @tool("get_health_history", "List logged health notes (injuries, illness, chronic-condition flares, scheduled procedures, or other temporary things affecting training). Use before making assumptions about the user's current health state.", {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(coach.VALID_HEALTH_STATUSES), "description": "Omit for all statuses"},
            "category": {"type": "string", "enum": list(coach.VALID_HEALTH_CATEGORIES), "description": "Omit for all categories"},
        },
    })
    async def get_health_history(args):
        result = _db_call(coach.list_health_notes, args.get("status"), args.get("category"), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("find_related_health_history", "Check whether a resolved injury exists for the same body area before logging a new one — call this for category='injury' before log_health_note so a recurring issue can be linked rather than treated as fresh.", {
        "type": "object",
        "properties": {"bodyArea": {"type": "string", "enum": list(coach.VALID_BODY_AREAS)}},
        "required": ["bodyArea"],
    })
    async def find_related_health_history(args):
        try:
            result = _db_call(coach.find_related_health_history, args["bodyArea"], user_id=user_id)
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("log_health_note", "Record something affecting training — an injury, illness, chronic-condition flare, scheduled procedure, or other temporary thing. body_area and relatedNoteId are only valid when category is 'injury'.", {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(coach.VALID_HEALTH_CATEGORIES)},
            "bodyArea": {"type": "string", "enum": list(coach.VALID_BODY_AREAS), "description": "Only for category='injury'"},
            "suspectedType": {"type": "string", "description": "e.g. 'ankle sprain', 'COVID', 'migraine', 'colonoscopy prep'"},
            "suspectedSeverity": {"type": "string", "enum": list(coach.VALID_SEVERITIES)},
            "trainingImpact": {"type": "string", "description": "What's actually restricted vs fine — e.g. 'avoid grip/strength work, running is fine'"},
            "expectedClearDate": {"type": "string", "description": "YYYY-MM-DD, your best-judgment estimate of when this should have cleared"},
            "notes": {"type": "string"},
            "relatedNoteId": {"type": "string", "description": "Only for category='injury' — id of a prior resolved note this appears to be a recurrence of"},
        },
        "required": ["category"],
    })
    async def log_health_note(args):
        try:
            result = _db_call(
                coach.log_health_note, args["category"],
                args.get("suspectedType"), args.get("suspectedSeverity"), args.get("trainingImpact"),
                args.get("expectedClearDate"), args.get("notes"), args.get("bodyArea"), args.get("relatedNoteId"),
                user_id=user_id, is_test=is_test,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("update_health_status", "Update a health note's status — e.g. mark it resolved once the user confirms it's cleared up.", {
        "type": "object",
        "properties": {
            "noteId": {"type": "string"},
            "status": {"type": "string", "enum": list(coach.VALID_HEALTH_STATUSES)},
            "notes": {"type": "string"},
        },
        "required": ["noteId", "status"],
    })
    async def update_health_status(args):
        try:
            result = _db_call(coach.update_health_status, args["noteId"], args["status"], args.get("notes"), user_id=user_id)
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_scheduled_workouts", "List scheduled/prescribed workouts. Planned workouts whose date has passed are auto-checked against real synced runs and linked/marked completed if a match is found.", {
        "type": "object",
        "properties": {
            "startDate": {"type": "string", "description": "YYYY-MM-DD"},
            "endDate": {"type": "string", "description": "YYYY-MM-DD"},
            "status": {"type": "string", "enum": list(coach.VALID_WORKOUT_STATUSES), "description": "Omit for all statuses"},
        },
    })
    async def get_scheduled_workouts(args):
        result = _db_call(coach.list_workouts, args.get("startDate"), args.get("endDate"), args.get("status"), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_exercise_progress", "Current progression state for a strength exercise (current weight/reps target/hold duration, last completed) — or sensible fresh-start defaults if it's never been prescribed before. Read-only: this is derived state the generator's double-progression rule owns; check it before proposing a challenge/ramp involving this exercise so you know whether it's already progressing.", {
        "type": "object",
        "properties": {"exercise": {"type": "string", "description": "e.g. 'Push-up', 'Plank'"}},
        "required": ["exercise"],
    })
    async def get_exercise_progress(args):
        result = _db_call(coach.get_exercise_progress, args["exercise"], user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    # Phase 12.3 found a real pre-existing gap during testing: this schema only ever
    # described the legacy generic shape, even though coach._validate_steps has
    # accepted the Phase 4.4 strength_exercise shape (stepType/restSeconds/sets) for
    # a while now — the chat tool just never exposed it, so the model could never
    # actually construct one, meaning every chat-scheduled strength session used the
    # generic shape and could never show a workout-runner "Start" button or feed real
    # ExerciseProgress double-progression. oneOf lets both shapes coexist in one field
    # without changing schedule_workout/update_workout's own top-level signature.
    STEPS_SCHEMA = {
        "type": "array",
        "description": "Structured step-by-step breakdown, in order. Use the generic shape for mobility/warmup/general circuits. Use the strength_exercise shape (stepType='strength_exercise') for any real strength/weight-training exercise with sets — this is required for it to show up in the workout runner (rest-timer + weight/rep logging UI) and to feed the exercise-progression system; the generic shape's plain 'reps' field does neither. Split a unilateral movement (leg swings, single-leg work, side plank) into two separate steps, one per side, rather than one step covering both.",
        "items": {
            "oneOf": [
                {
                    "type": "object",
                    "description": "Generic step — mobility/warmup/general circuit work.",
                    "properties": {
                        "exercise": {"type": "string", "description": "e.g. 'Leg swings', 'Side plank', 'Bird dog', '800m repeat'"},
                        "side": {"type": "string", "enum": list(coach.VALID_STEP_SIDES), "description": "Only for unilateral movements — omit for bilateral/whole-body steps."},
                        "durationSec": {"type": "integer", "description": "For time-based steps (holds, circuits, intervals)"},
                        "reps": {"type": "integer", "description": "For rep-based steps"},
                        "notes": {"type": "string", "description": "Short context for this step — why/when/conditional (e.g. 'optional, skip if fatigued', 'start of core circuit, repeat 2 rounds'). Not a how-to."},
                        "howTo": {"type": "string", "description": "Technique/form cues for actually performing this step — how to set up, what to feel, common mistakes to avoid. Include it for anything the user might not already know how to do correctly (stretches, mobility drills, unfamiliar exercises, foam-rolling). Skip it for self-explanatory steps (e.g. 'easy bike, zone 1 pace'). Rendered as an expandable detail in the Workouts tab, not shown inline, so don't hold back on length here."},
                    },
                    "required": ["exercise"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "description": "Strength exercise with real sets — required for the workout runner and exercise-progression tracking. restSeconds lives on the exercise (rest after each set), not per-set.",
                    "properties": {
                        "stepType": {"type": "string", "enum": ["strength_exercise"]},
                        "exercise": {"type": "string", "description": "e.g. 'Push-up', 'Plank', 'Goblet Squat'"},
                        "restSeconds": {"type": "integer", "description": "Rest after each set, in seconds"},
                        "sets": {
                            "type": "array",
                            "description": "One entry per set, in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer", "description": "0-based set number"},
                                    "targetType": {"type": "string", "enum": ["reps", "hold_sec"]},
                                    "targetReps": {"type": "integer", "description": "Required when targetType is 'reps'"},
                                    "targetHoldSec": {"type": "integer", "description": "Required when targetType is 'hold_sec'"},
                                    "targetWeightLb": {"type": "number", "description": "Omit for bodyweight-only sets"},
                                },
                                "required": ["targetType"],
                            },
                        },
                    },
                    "required": ["stepType", "exercise", "restSeconds", "sets"],
                    "additionalProperties": False,
                },
            ],
        },
    }

    @tool("schedule_workout", "Prescribe a workout for a specific date. Check get_health_history first — a workout suggestion must be modified (rest/lower intensity/avoid the affected area) if anything active would be affected, not just persona-toned around the same plan.", {
        "type": "object",
        "properties": {
            "scheduledDate": {"type": "string", "description": "YYYY-MM-DD"},
            "workoutType": {"type": "string", "enum": list(coach.VALID_WORKOUT_TYPES)},
            "activityType": {"type": "string", "description": "e.g. 'Run', 'Ride', 'Walk' — must match what the user actually does for this session so it can auto-link to the right synced activity later. Leave unset for rest/cross_train/strength days that aren't literally a run or ride (e.g. a mobility/core session) — it defaults to a non-linking placeholder so an unrelated activity synced that day can't wrongly mark it complete. Only set it explicitly when the cross-training itself is a trackable activity type, e.g. 'Ride' for a bike cross-training day."},
            "targetDistanceMi": {"type": "number"},
            "targetPaceSecPerMi": {"type": "integer"},
            "targetDurationSec": {"type": "integer", "description": "Total session length in seconds. If every step below is time-based (has durationSec), this is auto-filled from the sum of your steps when omitted, and rejected if you set it to something the steps don't actually add up to (e.g. don't call it \"20 minutes\" while only listing 3 minutes of steps — list enough steps/rounds to really fill the time, or state the real total instead)."},
            "notes": {"type": "string", "description": "Prescription rationale — keep this the high-level why; put the actual exercise-by-exercise plan in steps"},
            "steps": STEPS_SCHEMA,
        },
        "required": ["scheduledDate", "workoutType"],
    })
    async def schedule_workout(args):
        try:
            result = _db_call(
                coach.create_workout, args["scheduledDate"], args["workoutType"],
                args.get("activityType"), args.get("targetDistanceMi"),
                args.get("targetPaceSecPerMi"), args.get("targetDurationSec"), args.get("notes"),
                args.get("steps"), user_id=user_id, is_test=is_test,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("update_workout", "Update a scheduled workout — reschedule, change prescription, or mark skipped.", {
        "type": "object",
        "properties": {
            "workoutId": {"type": "string"},
            "scheduledDate": {"type": "string"},
            "workoutType": {"type": "string", "enum": list(coach.VALID_WORKOUT_TYPES)},
            "activityType": {"type": "string"},
            "targetDistanceMi": {"type": "number"},
            "targetPaceSecPerMi": {"type": "integer"},
            "targetDurationSec": {"type": "integer", "description": "Total session length in seconds. If every step below is time-based (has durationSec), this is auto-filled from the sum of your steps when omitted, and rejected if you set it to something the steps don't actually add up to (e.g. don't call it \"20 minutes\" while only listing 3 minutes of steps — list enough steps/rounds to really fill the time, or state the real total instead)."},
            "notes": {"type": "string"},
            "steps": STEPS_SCHEMA,
            "status": {"type": "string", "enum": list(coach.VALID_WORKOUT_STATUSES)},
        },
        "required": ["workoutId"],
    })
    async def update_workout(args):
        fields = {
            "scheduled_date": args.get("scheduledDate"), "workout_type": args.get("workoutType"),
            "activity_type": args.get("activityType"), "target_distance_mi": args.get("targetDistanceMi"),
            "target_pace_sec_per_mi": args.get("targetPaceSecPerMi"), "target_duration_sec": args.get("targetDurationSec"),
            "notes": args.get("notes"), "steps": args.get("steps"), "status": args.get("status"),
        }
        try:
            result = _db_call(coach.update_workout, args["workoutId"], user_id=user_id, **fields)
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("record_workout_completion", "Record a critique of a completed workout — pair it with a real run (from query_runs/get_run_detail) and your honest analysis of how it went relative to the plan or recent trends.", {
        "type": "object",
        "properties": {
            "workoutId": {"type": "string"},
            "runId": {"type": "string", "description": "The real Run this session corresponds to, if not already auto-linked"},
            "critiqueText": {"type": "string"},
        },
        "required": ["workoutId", "critiqueText"],
    })
    async def record_workout_completion(args):
        try:
            result = _db_call(coach.record_workout_completion, args["workoutId"], args.get("runId"),
                               args["critiqueText"], user_id=user_id)
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("log_product_feedback", "Call this INSTEAD OF a coaching answer when the user's message is a bug report, feature request, or general product/UX feedback about the HALE app itself — not a question about their own training/health/data. Summarize what they're asking for/reporting in your own words, specific enough to act on later. Do not deflect back to the user asking what they actually want or whether this is a coaching question — log it and move on. If a message mixes a real coaching question with product feedback, still answer the coaching part normally and also call this for the feedback part.", {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["bug", "feature_request", "feedback"]},
            "summary": {"type": "string", "description": "What they're asking for/reporting — specific enough that someone reading it later (not you) understands the request without needing the original message."},
        },
        "required": ["category", "summary"],
    })
    async def log_product_feedback(args):
        if is_test:
            # Never pollutes the real draft with verification traffic — mirrors the
            # HealthNote/Workout is_test guard, see Phase 12.1.
            return {"content": [{"type": "text", "text": "test mode — not logged"}]}
        from . import self_review
        # append_to_draft_async, not the sync append_to_draft — this coroutine is
        # already running inside the SDK's own event loop (we're mid client.query()),
        # and append_to_draft's asyncio.run() would raise "cannot be called from a
        # running event loop" if used here. Opens its own session rather than routing
        # through the sync-only _db_call helper, for the same reason.
        db = SessionLocal()
        try:
            await self_review.append_to_draft_async(
                db, user_id, "User feedback", f"**{args['category']}**: {args['summary']}", "live chat",
            )
        finally:
            db.close()
        return {"content": [{"type": "text", "text": "Logged."}]}

    @tool("get_recovery_tools", "List recovery devices the user owns (e.g. compression boots) and their supported level/duration/zone-boost ranges. Also included automatically in every message's context when the user has any on file — call this directly only if you need the full list again mid-conversation.", {
        "type": "object", "properties": {},
    })
    async def get_recovery_tools(args):
        result = _db_call(coach.list_recovery_tools, user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("recommend_recovery_session", "Prescribe a recovery-tool session (e.g. compression boots) for a specific date. level/durationMin must fall within that tool's supported range (see get_recovery_tools or the context block) — durationMin must also land on that tool's increment. zoneBoost only valid if the tool supports it.", {
        "type": "object",
        "properties": {
            "toolId": {"type": "string", "description": "id from get_recovery_tools or the context block"},
            "scheduledDate": {"type": "string", "description": "YYYY-MM-DD"},
            "level": {"type": "integer"},
            "durationMin": {"type": "integer"},
            "zoneBoost": {"type": "boolean", "description": "Default false"},
            "rationale": {"type": "string", "description": "Why this level/duration — e.g. 'legs were heavy after yesterday's long run'"},
        },
        "required": ["toolId", "scheduledDate", "level", "durationMin"],
    })
    async def recommend_recovery_session(args):
        try:
            result = _db_call(
                coach.recommend_recovery_session, args["toolId"], args["scheduledDate"],
                args["level"], args["durationMin"], args.get("zoneBoost", False),
                args.get("rationale"), user_id=user_id,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("get_recovery_sessions", "List recommended/logged recovery-tool sessions.", {
        "type": "object",
        "properties": {
            "startDate": {"type": "string", "description": "YYYY-MM-DD"},
            "endDate": {"type": "string", "description": "YYYY-MM-DD"},
            "status": {"type": "string", "enum": list(coach.VALID_RECOVERY_SESSION_STATUSES), "description": "Omit for all statuses"},
        },
    })
    async def get_recovery_sessions(args):
        result = _db_call(
            coach.list_recovery_sessions, args.get("startDate"), args.get("endDate"),
            args.get("status"), user_id=user_id,
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("render_chart", "Embed a chart in your reply. Use real numbers you already retrieved via another tool this turn (e.g. get_weekly_mileage) — this tool does not compute anything itself, it just declares what to draw. labels and each dataset's data array must be the same length.", {
        "type": "object",
        "properties": {
            "chartType": {"type": "string", "enum": ["line", "bar"]},
            "title": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "X-axis labels, e.g. dates or week-start strings"},
            "datasets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "data": {"type": "array", "items": {"type": "number"}},
                    },
                    "required": ["label", "data"],
                },
            },
        },
        "required": ["chartType", "title", "labels", "datasets"],
    })
    async def render_chart(args):
        # Pure passthrough — no DB call, no computation. Its "result" is just its own
        # input echoed back; send_message() pulls chart specs straight out of the
        # already-captured tool_calls list rather than needing to parse a separate
        # ToolResultBlock from the SDK stream.
        return {"content": [{"type": "text", "text": json.dumps(args)}]}

    @tool("search_chat_history", "Search chat history for relevant prior context from past conversations. Use natural language queries (e.g., 'shin injury', 'weight gain last month', 'compression boots') — returns up to 10 of the most recent matching messages, useful for cross-session continuity when the model needs context it wasn't given this turn.", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language search query (e.g., 'shin injury', 'recovery session', 'pace trend')"},
            "limit": {"type": "integer", "description": "Max results to return, default 10"},
        },
        "required": ["query"],
    })
    async def search_chat_history(args):
        result = _db_call(stats.search_chat_history, args["query"], args.get("limit", 10), user_id=user_id)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return [
        get_run_summary, get_weekly_mileage, get_monthly_mileage, get_personal_records,
        get_pace_trend, get_training_load_trend, get_readiness, get_daily_steps, query_runs, get_run_detail,
        get_health_history, find_related_health_history, log_health_note, update_health_status,
        get_scheduled_workouts, schedule_workout, update_workout, record_workout_completion,
        get_exercise_progress, log_product_feedback,
        render_chart, get_goals,
        get_recovery_tools, recommend_recovery_session, get_recovery_sessions,
        search_chat_history,
    ]


_clients: dict[tuple[str, bool], ClaudeSDKClient] = {}  # (user_id, is_test) -> client


def is_configured() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))


def _current_persona_inputs(user_id: str = DEFAULT_USER_ID) -> tuple[str, str | None]:
    """Both build_system_prompt inputs (personality, sex) in one DB round-trip —
    Phase 28 folded sex in here rather than a second db.get(User, ...) call."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        return ((user.coach_personality if user else None) or "normal", user.sex if user else None)
    finally:
        db.close()


async def _get_client(user_id: str = DEFAULT_USER_ID, is_test: bool = False) -> ClaudeSDKClient:
    """One SDK session per (user, is_test), not one shared session for the whole app —
    otherwise two real users' conversations would bleed into each other's live SDK-side
    context (the turn-by-turn memory the SDK keeps internally, separate from the
    ChatMessage history in SQLite, which was already correctly scoped per-user via
    owned_by()), and resetting one user's session (e.g. on a persona change) would
    silently blow away every other user's conversation too. `is_test` (Phase 12.1) is
    part of the cache key for the same reason, not just a tool-construction flag: since
    _build_tools' closures capture is_test at creation time, a session built once as
    real and reused for a later test message (or vice versa) would silently stamp every
    row with the WRONG value for the rest of that session's lifetime — keeping test
    traffic on a fully separate session also means it never pollutes the real
    conversation's own live in-SDK memory, not just the persisted rows. Options are
    built fresh per client rather than once at import time, since the system prompt
    depends on that user's coach_personality. reset_client() (below) forces a rebuild
    for one user (both variants) only — the next message from that user rebuilds with
    the new tone; other users' sessions are untouched."""
    key = (user_id, is_test)
    if key not in _clients:
        server = create_sdk_mcp_server(name="runlog", version="0.1.0", tools=_build_tools(user_id, is_test))
        options = ClaudeAgentOptions(
            mcp_servers={"runlog": server},
            allowed_tools=ALLOWED_TOOL_NAMES,
            disallowed_tools=BUILTIN_TOOLS_BLOCKLIST,
            system_prompt=coach.build_system_prompt(*_current_persona_inputs(user_id)),
            permission_mode="bypassPermissions",  # headless container, no TTY to prompt for approval
            setting_sources=[],  # don't inherit any local .claude/settings.json in the container
            max_turns=8,
            model="claude-haiku-4-5-20251001",  # cheapest current Claude model — the Coach's tool
                                                  # calls are narrow/well-described enough not to need
                                                  # a flagship model; revisit if tool selection or the
                                                  # health-note safety override starts misbehaving
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        _clients[key] = client
    return _clients[key]


async def reset_client(user_id: str = DEFAULT_USER_ID):
    for is_test in (False, True):
        client = _clients.pop((user_id, is_test), None)
        if client is not None:
            await client.disconnect()


def _persist(role: str, content: str, tool_calls=None, charts=None, user_id: str = DEFAULT_USER_ID,
             is_test: bool = False):
    db = SessionLocal()
    try:
        db.add(ChatMessage(
            user_id=user_id, role=role, content=content,
            tool_calls_json=json.dumps(tool_calls) if tool_calls else None,
            charts_json=json.dumps(charts) if charts else None,
            created_at=datetime.now(timezone.utc).isoformat(), is_test=is_test,
        ))
        db.commit()
    finally:
        db.close()


async def send_message(user_text: str, user_id: str = DEFAULT_USER_ID, is_test: bool = False,
                        activity_id: str | None = None) -> dict:
    """Non-streaming: blocks for the full response, persists both sides, returns
    {reply, toolCalls}. toolCalls records which real queries backed the answer, for
    UI transparency — directly serving the "grounded, not hallucinated" requirement.
    `is_test` (Phase 12.1) is set from the X-Hale-Test header on the incoming request —
    routes/chat.py is the only caller that ever passes it True. `activity_id` (Phase 26)
    is set only when the message originated from an activity card's "Ask coach" button —
    it grounds the reply on that specific run via a hidden context block, same mechanism
    as health/recovery context below."""
    if not is_configured():
        raise RuntimeError("AI assistant not configured — set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY")

    _persist("user", user_text, user_id=user_id, is_test=is_test)  # persist the ORIGINAL text
                                  # only — the health context block below is injected into what
                                  # the model sees, never into chat history or the UI
    db = SessionLocal()
    try:
        health_context = coach.get_health_context_block(db, user_id)
        recovery_context = coach.get_recovery_tools_context_block(db, user_id)
        activity_context = coach.get_activity_context_block(db, activity_id, user_id)
    finally:
        db.close()

    client = await _get_client(user_id, is_test)
    await client.query(coach.get_date_context_block(user_id) + health_context + recovery_context
                        + activity_context + user_text)

    reply_text = ""
    tool_calls = []
    async for msg in client.receive_response():
        msg_type = type(msg).__name__
        if msg_type == "AssistantMessage":
            if getattr(msg, "error", None):
                raise RuntimeError(f"Assistant error: {msg.error}")
            for block in msg.content:
                block_type = type(block).__name__
                if block_type == "TextBlock":
                    reply_text += block.text
                elif block_type == "ToolUseBlock" and str(block.name).startswith("mcp__runlog__"):
                    tool_calls.append({"tool": block.name.replace("mcp__runlog__", ""), "input": block.input})

    # render_chart is a pure passthrough (see _build_tools) — its "result" is exactly
    # its own input, already captured above via the normal ToolUseBlock handling, so
    # chart specs are pulled straight out of tool_calls rather than needing a second
    # pass over the SDK's tool-result message stream.
    charts = [tc["input"] for tc in tool_calls if tc["tool"] == "render_chart"]

    _persist("assistant", reply_text, tool_calls, charts, user_id=user_id, is_test=is_test)
    return {"reply": reply_text, "toolCalls": tool_calls, "charts": charts}
