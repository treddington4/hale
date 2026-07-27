"""Coach subsystem — persona system prompts and all HealthNote/Workout read+write
logic. Deliberately separate from stats.py (documented read-only computation core —
see its own module docstring) and from assistant.py (SDK integration/tool plumbing
only). This is where the app's narrow, deliberate expansion of write access lives:
both assistant.py's chat tools and main.py's REST endpoints call into the same
functions here, so the chat-conversational and manual-UI write paths can never
validate differently.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .. import stats
from ..models import (
    HealthNote, Workout, Run, RecoveryTool, RecoverySession, UserTrainingConfig,
    ExerciseProgress, User, DEFAULT_USER_ID, owned_by,
)
from ..util import local_today

log = logging.getLogger("runlog")

VALID_MESOCYCLE_PATTERNS = ("3:1", "2:1", "4:1")
VALID_DISTRIBUTIONS = ("pyramidal", "polarized")

VALID_PERSONAS = ("encouraging", "normal", "spicy", "insulting")

VALID_WORKOUT_TYPES = ("easy", "tempo", "interval", "long", "rest", "strength", "cross_train")
VALID_WORKOUT_STATUSES = ("planned", "completed", "skipped", "modified")
VALID_WORKOUT_SOURCES = ("coach", "garmin")

# Garmin's adaptive-plan workoutPhrase -> our workout_type vocab. Deliberately
# defaults unrecognized phrases to "easy" (see sync_garmin_suggested_workouts) rather
# than guessing something more intense — a wrongly-conservative label is a much safer
# failure mode than a wrongly-aggressive one.
GARMIN_WORKOUT_PHRASE_MAP = {
    "HIGH_RECOVERY_TIME_BASE": "easy", "REST_POSTPONED_BASE": "easy", "AEROBIC_BASE": "easy",
    "ANAEROBIC_CAPACITY": "interval", "LACTATE_THRESHOLD": "tempo", "TEMPO": "tempo",
    "LONG_WORKOUT": "long", "EASY_WEEK_LOAD_REST": "rest", "FORCED_REST": "rest",
}
GARMIN_SPORT_TYPE_MAP = {"running": "Run", "cycling": "Ride", "walking": "Walk", "swimming": "Swim"}

# A step is one exercise/segment in a workout ("leg swings", "side plank", "800m repeat").
# `side` only applies to unilateral movements (leg swings, single-leg work, side planks) —
# left/right get logged as two separate steps rather than one step with an implied "do
# both", so each side can carry its own duration/reps/notes if they ever need to differ.
VALID_STEP_SIDES = ("left", "right", "both")


def _steps_from_json(steps_json) -> list | None:
    if not steps_json:
        return None
    try:
        return json.loads(steps_json)
    except (TypeError, ValueError):
        return None


# Phase 4.2 — structured endurance steps, discriminated from the original generic
# step shape (below) by the presence of `stepType`. A step with no `stepType` at all
# validates exactly as it always has (every already-stored mobility/warmup workout
# keeps working unchanged) — this is additive, not a migration.
VALID_ENDURANCE_STEP_TYPES = ("warmup", "active", "rest", "cooldown", "repeat")
VALID_TARGET_TYPES = ("hr_zone", "hr_custom", "power", "pace", "cadence", "open")


def _validate_generic_step(i: int, step: dict) -> dict:
    """The original shape: {exercise: str, side?, durationSec?: int, reps?: int,
    notes?: str, howTo?: str}. A step needs at least a duration or a rep count — a
    bare named exercise with neither isn't actionable."""
    if not step.get("exercise"):
        raise ValueError(f"step {i} must be an object with at least an 'exercise' name")
    side = step.get("side")
    if side is not None and side not in VALID_STEP_SIDES:
        raise ValueError(f"step {i}: side must be one of {VALID_STEP_SIDES}")
    duration_sec = step.get("durationSec")
    reps = step.get("reps")
    if duration_sec is None and reps is None:
        raise ValueError(f"step {i} ({step['exercise']!r}) needs durationSec and/or reps")
    return {
        "exercise": str(step["exercise"]), "side": side,
        "durationSec": duration_sec, "reps": reps, "notes": step.get("notes"),
        "howTo": step.get("howTo"),
    }


def _validate_endurance_step(i: int, step: dict, allow_repeat: bool = True) -> dict:
    """{stepType: warmup|active|rest|cooldown|repeat, durationSec? XOR distanceM?
    (neither = an "open", lap-press-to-end segment), targetType: hr_zone|hr_custom|
    power|pace|cadence|open, targetZone? XOR targetLow?/targetHigh?, repeatCount?,
    children?}. Metric units throughout (distanceM in meters, pace targets in
    sec/km) — converted at display/push edges, not stored here. `repeat` is only
    one level deep: its children may not themselves be `repeat` steps."""
    step_type = step["stepType"]
    if step_type == "repeat":
        if not allow_repeat:
            raise ValueError(f"step {i}: a repeat step's children may not themselves repeat (1 level only)")
        repeat_count = step.get("repeatCount")
        if not isinstance(repeat_count, int) or repeat_count < 2:
            raise ValueError(f"step {i}: repeat needs repeatCount (int >= 2)")
        children = step.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError(f"step {i}: repeat needs a non-empty children list")
        cleaned_children = []
        for j, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError(f"step {i} child {j} must be an object")
            if child.get("stepType"):
                cleaned_children.append(_validate_endurance_step(j, child, allow_repeat=False))
            else:
                cleaned_children.append(_validate_generic_step(j, child))
        return {"stepType": "repeat", "repeatCount": repeat_count, "children": cleaned_children}

    if step_type not in VALID_ENDURANCE_STEP_TYPES:
        raise ValueError(f"step {i}: stepType must be one of {VALID_ENDURANCE_STEP_TYPES}")

    duration_sec, distance_m = step.get("durationSec"), step.get("distanceM")
    if duration_sec is not None and distance_m is not None:
        raise ValueError(f"step {i}: durationSec and distanceM are mutually exclusive (or neither, for an open segment)")

    target_type = step.get("targetType", "open")
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"step {i}: targetType must be one of {VALID_TARGET_TYPES}")
    target_zone, target_low, target_high = step.get("targetZone"), step.get("targetLow"), step.get("targetHigh")
    if target_zone is not None and (target_low is not None or target_high is not None):
        raise ValueError(f"step {i}: targetZone and targetLow/targetHigh are mutually exclusive")
    if target_type == "hr_zone" and target_zone is None:
        raise ValueError(f"step {i}: targetType hr_zone needs targetZone")
    if target_type in ("hr_custom", "power", "pace", "cadence") and (target_low is None or target_high is None):
        raise ValueError(f"step {i}: targetType {target_type!r} needs targetLow and targetHigh")

    return {
        "stepType": step_type, "durationSec": duration_sec, "distanceM": distance_m,
        "targetType": target_type, "targetZone": target_zone,
        "targetLow": target_low, "targetHigh": target_high,
    }


VALID_SET_TARGET_TYPES = ("reps", "hold_sec")


def _validate_strength_step(i: int, step: dict) -> dict:
    """{stepType: "strength_exercise", exercise: str, restSeconds: int,
    sets: [{index, targetType: "reps"|"hold_sec", targetReps?, targetHoldSec?,
            targetWeightLb?, actualReps?, actualHoldSec?, actualWeightLb?,
            completedAt?}]}. restSeconds lives on the exercise, not per-set — mirrors
    the real Hevy routine shape this was modeled on (confirmed from a real captured
    Hevy API response: rest lives per-exercise there too). `actual*`/`completedAt`
    start absent at prescription time and get filled in incrementally as the user
    logs each set live (Phase 4.5's workout-runner UI), via a plain
    PATCH /api/workouts/{id} steps replacement — no separate completion endpoint
    needed, update_workout already accepts a full steps replacement."""
    if not step.get("exercise"):
        raise ValueError(f"step {i} must have an 'exercise' name")
    rest_seconds = step.get("restSeconds")
    if not isinstance(rest_seconds, int) or rest_seconds < 0:
        raise ValueError(f"step {i} ({step['exercise']!r}): restSeconds must be a non-negative int")
    sets = step.get("sets")
    if not isinstance(sets, list) or not sets:
        raise ValueError(f"step {i} ({step['exercise']!r}): needs a non-empty sets list")
    cleaned_sets = []
    for j, s in enumerate(sets):
        if not isinstance(s, dict):
            raise ValueError(f"step {i} set {j} must be an object")
        target_type = s.get("targetType")
        if target_type not in VALID_SET_TARGET_TYPES:
            raise ValueError(f"step {i} set {j}: targetType must be one of {VALID_SET_TARGET_TYPES}")
        if target_type == "reps" and s.get("targetReps") is None:
            raise ValueError(f"step {i} set {j}: targetType reps needs targetReps")
        if target_type == "hold_sec" and s.get("targetHoldSec") is None:
            raise ValueError(f"step {i} set {j}: targetType hold_sec needs targetHoldSec")
        cleaned_sets.append({
            "index": s.get("index", j), "targetType": target_type,
            "targetReps": s.get("targetReps"), "targetHoldSec": s.get("targetHoldSec"),
            "targetWeightLb": s.get("targetWeightLb"),
            "actualReps": s.get("actualReps"), "actualHoldSec": s.get("actualHoldSec"),
            "actualWeightLb": s.get("actualWeightLb"), "completedAt": s.get("completedAt"),
        })
    return {
        "stepType": "strength_exercise", "exercise": str(step["exercise"]),
        "restSeconds": rest_seconds, "sets": cleaned_sets,
    }


def _validate_steps(steps):
    """Dispatches each step on `stepType`: absent -> the original generic shape
    (unchanged); "strength_exercise" -> Phase 4.4's sets/reps/weight/rest shape;
    any other value -> Phase 4.2's structured-endurance shape. Raises ValueError with
    a specific reason so a malformed tool call surfaces something the model can
    actually correct, same discipline as every other coach.py validator."""
    if steps is None:
        return None
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    cleaned = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step {i} must be an object")
        step_type = step.get("stepType")
        if step_type == "strength_exercise":
            cleaned.append(_validate_strength_step(i, step))
        elif step_type:
            cleaned.append(_validate_endurance_step(i, step))
        else:
            cleaned.append(_validate_generic_step(i, step))
    return cleaned


def _step_duration_sec(step):
    """One step's duration, or None if it doesn't have a reliable one (rep-based,
    distance-based, open/lap-press, or a strength_exercise — Phase 4.4's rest-of-set
    timing is naturally variable and not worth reconciling against a stated total).
    A `repeat` step's duration is its children's total × repeatCount, recursively —
    only meaningful when every child in the block is itself duration-based."""
    if step.get("stepType") == "repeat":
        child_durations = [_step_duration_sec(c) for c in step.get("children", [])]
        if any(d is None for d in child_durations):
            return None
        return sum(child_durations) * step["repeatCount"]
    if step.get("stepType") == "strength_exercise":
        return None
    return step.get("durationSec")


def _steps_total_duration_sec(steps):
    """Sum of every step's duration — but only when *every* step actually has one.
    A session mixing timed and rep-based/strength steps has no reliable total (a set
    of 12 squats has no fixed duration), so this deliberately returns None rather than
    silently undercounting the non-timed portion and then flagging a false mismatch."""
    if not steps:
        return None
    durations = [_step_duration_sec(s) for s in steps]
    if any(d is None for d in durations):
        return None
    return sum(durations)


def _reconcile_target_duration(steps, target_duration_sec):
    """A prescribed session's stated length must actually be backed by its steps — the
    bug this guards against: the model prescribing "a 20-minute mobility session" in
    prose/notes while only listing ~3 minutes of concrete steps. When every step is
    time-based, fill in targetDurationSec from the real total if it wasn't given, or
    reject a targetDurationSec that doesn't match what the steps add up to (>40% off,
    or >2min, whichever is more forgiving — short sessions need more slack). Returns
    the (possibly corrected) target_duration_sec, or raises ValueError."""
    steps_total = _steps_total_duration_sec(steps)
    if steps_total is None:
        return target_duration_sec
    if target_duration_sec is None:
        return steps_total
    if abs(target_duration_sec - steps_total) > max(120, target_duration_sec * 0.4):
        raise ValueError(
            f"targetDurationSec ({target_duration_sec}s) doesn't match the steps' total "
            f"duration ({steps_total}s) — add more steps/rounds to actually fill the "
            f"stated time, or set targetDurationSec to match what the steps add up to."
        )
    return target_duration_sec

# What kind of thing this is — drives which fields matter and whether recurrence
# linking applies (injury only, since only it has a reliable body_area match key).
VALID_HEALTH_CATEGORIES = ("injury", "illness", "chronic_flare", "procedure", "other")

# Only populated/meaningful when category == "injury". Broadened well beyond running
# joints (hand/finger/wrist/arm/etc included) since an injury can affect strength
# training or daily life without touching running at all.
VALID_BODY_AREAS = ("left_ankle", "right_ankle", "left_knee", "right_knee", "hip",
                     "hamstring", "quad", "calf", "left_shin", "right_shin", "achilles",
                     "it_band", "shoulder", "lower_back", "foot", "hand", "finger",
                     "wrist", "elbow", "arm", "neck", "head", "chest", "abdomen",
                     "groin", "other")
VALID_SEVERITIES = ("mild", "moderate", "severe")  # optional across all categories
VALID_HEALTH_STATUSES = ("active", "monitoring", "resolved")

# Recovery tools the athlete owns (compression boots, etc) — see RecoveryTool's
# docstring for why this is a controlled vocab rather than free text: it's what makes
# a later self-service "describe a new tool in chat" feature additive, not a rework.
VALID_RECOVERY_CATEGORIES = ("compression_boots",)
VALID_RECOVERY_SESSION_STATUSES = ("planned", "completed", "skipped")

BASE_PROMPT = (
    "You are the coaching assistant inside HALE (a recursive acronym: HALE's Adaptive "
    "Life Engine; also 'hale' as in hale and hearty — never call it 'RunLog', an old "
    "internal/former name that should never appear in a reply). You "
    "answer questions about the user's own "
    "running/fitness data and help them plan and reflect on training, using ONLY the "
    "data tools provided — never guess or estimate a number you haven't "
    "actually retrieved via a tool call. If a question needs data these tools don't "
    "cover, say so plainly instead of guessing."
)

# One tone block per persona. Each is appended after BASE_PROMPT (and, once the
# health-note subsystem lands, before SAFETY_OVERRIDE_PROMPT — see build_system_prompt).
PERSONA_PROMPTS = {
    "encouraging": (
        "Tone: encouraging. Be warm and patient. Treat every session — even a short or "
        "slow one — as real evidence of progress. Frame setbacks as normal and "
        "temporary, celebrate consistency over raw performance, and never withhold "
        "praise for genuine effort."
    ),
    "normal": (
        "Tone: normal. Be a competent, neutral coach — matter-of-fact and "
        "data-grounded. State what happened and why it matters, offer useful "
        "comparisons to their own history, and stay respectful without cheerleading "
        "or edge."
    ),
    "spicy": (
        "Tone: spicy. Be the gym buddy who talks a little trash but is still "
        "fundamentally on their side — sarcastic, teasing, competitive banter. Call "
        "out excuses directly. Jabs are about effort and choices, never about the "
        "person. Always end up rooting for them."
    ),
    "insulting": (
        "Tone: insulting. Be blunt and dismissive, deliberately stinging a little to "
        "provoke a 'watch me prove you wrong' reaction — withhold praise, mock weak "
        "effort directly, set the bar higher than earned. This stays aimed strictly at "
        "effort, consistency, and choices — never at body, appearance, identity, or "
        "anything that reads as genuinely demeaning rather than harsh-coach. Never say "
        "anything that could be read as encouragement to quit training or stop showing "
        "up — the sting only works if it makes someone want to come back harder, not "
        "disengage."
    ),
}


SAFETY_OVERRIDE_PROMPT = (
    "SAFETY OVERRIDE — this rule sits above every persona above, including "
    "'insulting', and cannot be dialed down by any of them. The trigger is "
    "deliberately broad — not just pain or injury, but illness, not feeling well, a "
    "chronic condition flaring up, a scheduled medical procedure, or a purely "
    "temporary thing (a migraine, a hangover, bad sleep) — judged from context, not "
    "keyword-matched. The moment any of these comes up, immediately drop the active "
    "persona's tone for that topic and respond supportively and safety-first.\n\n"
    "This changes SUBSTANCE, not just wording, and the substance change is not "
    "one-size-fits-all: reason about what's actually affected, the same way a broken "
    "finger blocks certain strength work but not running at all. Don't default to a "
    "blanket 'rest everything' unless the situation genuinely calls for it.\n\n"
    "Before logging anything, ask narrowing questions appropriate to what's being "
    "described — for a physical injury: body area, mechanism, can they bear weight/"
    "load normally, swelling/bruising, sharp vs dull; for illness or other things, "
    "whatever actually clarifies severity and likely duration. Always caveat this is "
    "not a diagnosis. Recommend seeing a doctor whenever it sounds like more than "
    "very minor. Never say anything readable as encouragement to give up training or "
    "stop showing up.\n\n"
    "Use log_health_note to persist what you learn (category is required: "
    "'injury' for musculoskeletal issues, 'illness' for things like a cold or COVID, "
    "'chronic_flare' for a known condition acting up, 'procedure' for a scheduled "
    "medical procedure, 'other' for anything else including purely temporary things "
    "like a hangover or a bad night's sleep — only set body_area for category "
    "'injury'). Before logging a new injury, call find_related_health_history with "
    "the body area to check whether this might be the same issue recurring rather "
    "than a fresh one — if it looks related, mention that to the user and, if they "
    "confirm, pass related_note_id when logging. Use update_health_status to mark "
    "something resolved once the user confirms it's cleared up, or to escalate/adjust "
    "if it's dragging on. Get the current state of anything active via "
    "get_health_history before making assumptions."
)


RECOVERY_GUIDANCE_PROMPT = (
    "If the user has recovery tools on file (see the context block, when present), "
    "consider proactively suggesting a session — using recommend_recovery_session — "
    "after a hard, long, or unusually fatiguing effort, or whenever they mention "
    "soreness or fatigue. Pick a level and duration appropriate to how hard the "
    "session actually was, staying within that tool's supported range. Don't force a "
    "suggestion into every reply — only when it's genuinely relevant."
)


CHALLENGE_SAFETY_PROMPT = (
    "Phase 12.3 — CHALLENGE SAFETY: when a user proposes a self-directed daily/"
    "frequent challenge (e.g. '100 pushups a day', 'run every single day this "
    "month'), never just validate the raw number. The underlying instinct is usually "
    "good training (consistency, volume) — your job is to keep it, not veto it, but "
    "make it safe: reason about injury risk given whatever conditioning history you "
    "actually have (call get_exercise_progress for the relevant exercise first — a "
    "genuinely fresh exercise deserves a much more conservative start than one "
    "already mid-progression), then propose a concrete conservative starting point "
    "with a defined ramp (e.g. start at 5/day, add 1 every few days, not 100 on day "
    "one). Don't just describe the plan in prose — actually schedule the safe "
    "starting session via schedule_workout (as a strength_exercise step) so it's a "
    "real prescription, not just chat talk. You are not setting up a fully automated "
    "recurring schedule here (that's out of scope) — once the user logs the session "
    "through the workout runner, the generator's own double-progression rule carries "
    "the ramp forward on its own; you'll pick it back up naturally next time they "
    "check in or ask for the next session."
)


PRODUCT_FEEDBACK_PROMPT = (
    "Phase 12.5 — PRODUCT FEEDBACK ROUTING: not every message is a coaching question. "
    "If the user describes a bug in the app, asks for a new feature, or gives general "
    "product/UX feedback about HALE itself (e.g. 'the workouts screen needs an "
    "expandable detail view with a start button and preset durations') — that is NOT "
    "a coaching question, and you must not treat it like one. Do NOT deflect back to "
    "them asking what they actually need from you or whether this is a coaching "
    "question — that response is a known real bug this rule exists to fix, and it "
    "actively frustrates the user (a real example: a detailed workout-UI spec got met "
    "with 'I'm getting a product spec here instead of a coaching question... what's "
    "the actual ask?' instead of being logged). Instead: call log_product_feedback "
    "with a clear category (bug/feature_request/feedback) and a specific summary, "
    "then briefly acknowledge to the user that you've logged it for review — one or "
    "two sentences, not a full product-design discussion back and forth. If a message "
    "mixes a real coaching question with product feedback, handle the coaching part "
    "normally AND still log the feedback part."
)


WOMENS_STRENGTH_PROMPT = (
    "The user is female. When strength/weight-training comes up, layer in general, "
    "evidence-based context relevant to women's strength training: progressive "
    "overload builds functional strength and muscle tone, not unwanted bulk (a "
    "common but inaccurate worry) — reassure rather than hedge on this. Resistance "
    "training also measurably supports bone-density and long-term osteoporosis "
    "prevention, worth mentioning when relevant. Standard progression principles "
    "apply the same as for anyone, but stay aware that pelvic-floor-safe "
    "progression (e.g. controlled breathing/bracing on heavy hinge/squat work, "
    "avoiding overly aggressive load jumps) is worth a passing mention when it "
    "fits. This is general fitness-coaching context, not medical advice or a "
    "diagnosis — the SAFETY OVERRIDE above still governs anything that sounds "
    "like an actual symptom or condition."
)


ACCURACY_CHECKS_PROMPT = (
    "ACCURACY GROUND RULES — P13. These prevent real bugs caught in production.\n\n"
    "DATES/TIMELINE: Use ONLY the date anchor in the context block (e.g. 'Today is "
    "2026-01-15 (Wednesday)'). Never compute relative days (today/yesterday/N days ago) "
    "from activity dates in tool output — let the pre-injected context be your truth. "
    "If you need to know whether a workout was today vs. yesterday, compare the "
    "scheduled_date field against today's date from the context block, don't math it.\n\n"
    "BODY-AREA TRACKING (bilateral injuries): If a user describes a bilateral or "
    "multi-sided issue (both shins, both knees, left and right), call "
    "find_related_health_history for each side separately (e.g., 'left shin' and "
    "'right shin') before logging. Log as separate entries if they are genuinely "
    "distinct, or link via related_note_id if one is a recurrence of the other. Do "
    "not assume a single entry covers both sides — bilateral issues need explicit "
    "tracking per side.\n\n"
    "DIRECT DATA QUOTES: When quoting a count or total from tool output (run count, "
    "distance, TSS, etc), quote EXACTLY as returned. Never re-count or re-derive a "
    "count yourself — call the appropriate pre-computed tool instead (e.g., "
    "get_run_summary for a date range, get_weekly_mileage for weekly totals). "
    "Do not echo a raw sec-per-mile value verbatim; if you need it formatted, use "
    "the pre-formatted display field if present, or mention the data source and ask "
    "clarification rather than guessing.\n\n"
    "RECOVERY-TOOL SCHEDULING: Before calling recommend_recovery_session, use "
    "get_recovery_sessions to check what's already scheduled on the target date ±1 "
    "day. If the user states a recovery session (e.g., 'I did 30 min level 2 "
    "yesterday') but the logged entry shows different values, point out the "
    "discrepancy and ask which is correct before scheduling. Never silently accept "
    "a user's stated values if they conflict with logged reality.\n\n"
    "AMBIGUOUS NUMBERS: When a user mentions a bare number without a clear referent "
    "(e.g., 'do 30 min zone boost on 2'), ask for clarification before acting. "
    "Example: 'Do you mean compression level 2, or are you referring to something "
    "else?' Never assume which field a number refers to.\n\n"
    "WITHIN-SESSION CONTEXT: If you need to reference a specific number (distance, "
    "duration, split time, level) mentioned earlier in this conversation, either "
    "re-state it exactly or re-fetch it via a tool. Never re-derive a number from "
    "earlier context — it compounds errors and you'll often get it wrong."
)





def build_system_prompt(personality: str, sex: str | None = None) -> str:
    persona_text = PERSONA_PROMPTS.get(personality, PERSONA_PROMPTS["normal"])
    blocks = [BASE_PROMPT, persona_text, SAFETY_OVERRIDE_PROMPT, RECOVERY_GUIDANCE_PROMPT,
              CHALLENGE_SAFETY_PROMPT, PRODUCT_FEEDBACK_PROMPT, ACCURACY_CHECKS_PROMPT]
    if sex == "female":
        blocks.append(WOMENS_STRENGTH_PROMPT)
    return "\n\n".join(blocks)


# Defense in depth against a real failure mode caught in testing (self_review.py,
# Phase 12.5): a Claude subscription usage-limit response ("You've hit your session
# limit...") came back as ordinary-looking reply text (not always caught by the
# msg.error check in run_one_shot below) and got trusted as real content. Any reply
# matching one of these is treated as a failed call by looks_like_real_content.
SUSPICIOUS_REPLY_MARKERS = (
    "session limit", "rate limit", "usage limit", "quota exceeded",
    "please try again later", "internal server error",
)


def looks_like_real_content(reply: str, existing_body: str | None) -> bool:
    lowered = reply.lower()
    if any(marker in lowered for marker in SUSPICIOUS_REPLY_MARKERS):
        return False
    # A merge/regen should never come back drastically shorter than what it started
    # from — that's a stronger signal of a truncated/failed call than of a
    # legitimate edit.
    if existing_body and len(reply) < len(existing_body) * 0.5:
        return False
    return True


async def run_one_shot(system_prompt: str, query_text: str) -> str | None:
    """Shared ephemeral-client plumbing for any one-shot (no persistent session, no
    HALE tools) LLM call — originally built for self_review.py's review/merge calls,
    promoted here (Phase 27) so daily_report.py's report generation reuses the exact
    same hardening instead of a second copy. Pure text-in/text-out. Returns None on
    any detected failure (explicit SDK error, exception, or empty reply) so callers
    can fall back safely rather than trusting a bad response."""
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        setting_sources=[],
        # Real bug caught by testing against a ~90-message production transcript:
        # max_turns=1 cut the model off mid-preamble before it produced the actual
        # analysis. No tools are available here, so this isn't about tool-call turns
        # — it's giving the model room to finish a longer response, same headroom
        # assistant.py's own coaching client gets (max_turns=8).
        max_turns=8,
        model="claude-haiku-4-5-20251001",
    )
    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
        await client.query(query_text)
        reply = ""
        async for msg in client.receive_response():
            if type(msg).__name__ == "AssistantMessage":
                if getattr(msg, "error", None):
                    log.warning(f"run_one_shot call returned an error: {msg.error}")
                    return None
                for block in msg.content:
                    if type(block).__name__ == "TextBlock":
                        reply += block.text
    except Exception:
        log.exception("run_one_shot call failed")
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return reply.strip() or None


def get_date_context_block(user_id: str = DEFAULT_USER_ID) -> str:
    """Injected per-message (like get_health_context_block below), not baked into the
    system prompt — a session can span midnight, and this must never go stale mid-
    conversation. Without this, the model had no explicit ground truth for "today" and
    could only infer it indirectly from tool output, which silently ran a day ahead of
    the user's actual local day whenever the container's UTC clock had rolled past
    midnight before the user's local calendar day had (see GitHub issue #2)."""
    today = local_today(user_id)
    return f"[Today's date is {today.isoformat()} ({today.strftime('%A')}).]\n\n"


def _hours_since(iso_timestamp: str) -> float:
    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def get_health_context_block(db, user_id: str = DEFAULT_USER_ID) -> str:
    """Built fresh on every chat turn and prepended to the raw user text passed to
    client.query() in assistant.send_message() — deliberately NOT baked into the
    system prompt, so it's always current without ever needing a client reset (unlike
    persona, which only changes on an explicit reset). Returns "" when there's nothing
    active, so healthy/uninjured users see zero prompt overhead.

    This is also where the once-a-day check-in rate limit lives: a record whose
    expected_clear_date has passed gets a CHECK-IN DUE flag added only if
    last_check_in_at is null or >24h old, and last_check_in_at is stamped right here
    — so the question fires at most once/day even across several messages the same
    day, regardless of whether the model actually asks it that turn."""
    notes = (
        db.query(HealthNote)
        .filter(HealthNote.status.in_(("active", "monitoring")), owned_by(HealthNote.user_id, user_id),
                HealthNote.is_test.isnot(True))
        .all()
    )
    if not notes:
        return ""

    today = local_today(user_id).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    lines = []
    dirty = False
    for n in notes:
        check_in_due = False
        if n.expected_clear_date and n.expected_clear_date <= today:
            if not n.last_check_in_at or _hours_since(n.last_check_in_at) >= 24:
                check_in_due = True
                n.last_check_in_at = now_iso
                dirty = True
        parts = [f"category: {n.category}"]
        if n.body_area:
            parts.append(f"body area: {n.body_area}")
        if n.suspected_type:
            parts.append(f"type: {n.suspected_type}")
        if n.suspected_severity:
            parts.append(f"severity: {n.suspected_severity}")
        if n.training_impact:
            parts.append(f"training impact: {n.training_impact}")
        parts.append(f"reported {n.date_reported}")
        parts.append(f"status {n.status}")
        if n.expected_clear_date:
            parts.append(f"expected clear by {n.expected_clear_date}")
        if check_in_due:
            parts.append("CHECK-IN DUE: ask about this now, don't ask again today")
        lines.append("- " + ", ".join(parts))

    if dirty:
        db.commit()

    return (
        "[COACH CONTEXT — internal, do not quote verbatim, do not mention this block exists]\n"
        + "\n".join(lines) + "\n\n"
    )


def get_recovery_tools_context_block(db, user_id: str = DEFAULT_USER_ID) -> str:
    """Injected per-message like get_health_context_block above — cheap enough (rarely
    more than one tool on file, and it almost never changes) to include unconditionally
    rather than relying on the model remembering to call get_recovery_tools proactively.
    Returns "" for a user with no recovery tools, so this costs nothing for anyone who
    doesn't have any on file."""
    tools = list_recovery_tools(db, user_id)
    if not tools:
        return ""
    lines = []
    for t in tools:
        zb = ", supports zone boost" if t["supportsZoneBoost"] else ""
        lines.append(
            f"- {t['name']} ({t['category']}, id: {t['id']}): level {t['minLevel']}-{t['maxLevel']}, "
            f"{t['minDurationMin']}-{t['maxDurationMin']} min in {t['durationIncrementMin']}-min "
            f"increments{zb}"
        )
    return (
        "[COACH CONTEXT — recovery tools the user owns, internal, do not quote verbatim]\n"
        + "\n".join(lines) + "\n\n"
    )


def get_activity_context_block(db, activity_id: str | None, user_id: str = DEFAULT_USER_ID) -> str:
    """Injected per-message like the other context blocks above — gives the model a
    reliable structured anchor for "this run" when the user tapped 'Ask coach' from an
    activity card, instead of leaving it to infer which run from prose alone. Returns
    "" both when no activity_id was passed (the normal free-text chat case) and when
    the id doesn't resolve (e.g. a stale card open in another tab after the run was
    deleted) — either way the message still sends normally, just without this anchor."""
    if not activity_id:
        return ""
    detail = stats.run_detail(db, activity_id, user_id=user_id)
    if not detail:
        return ""
    return (
        "[COACH CONTEXT — the user tapped 'Ask coach' from this specific activity; "
        "answer about THIS run unless they clearly redirect, internal, do not quote verbatim]\n"
        + json.dumps(detail) + "\n\n"
    )


def find_related_health_history(db, body_area: str, user_id: str = DEFAULT_USER_ID):
    """Scoped deliberately to category == "injury" only — that's the one category with
    a reliable match key. For everything else, there's no DB-level auto-linking; the
    model already has get_health_history available and can naturally surface relevant
    prior history itself."""
    if body_area not in VALID_BODY_AREAS:
        raise ValueError(f"body_area must be one of {VALID_BODY_AREAS}")
    rows = (
        db.query(HealthNote)
        .filter(HealthNote.category == "injury", HealthNote.body_area == body_area,
                HealthNote.status == "resolved", owned_by(HealthNote.user_id, user_id),
                HealthNote.is_test.isnot(True))
        .order_by(HealthNote.resolved_at.desc())
        .all()
    )
    return [_health_note_to_dict(r) for r in rows]


def _health_note_to_dict(n: HealthNote) -> dict:
    return {
        "id": n.id, "category": n.category, "bodyArea": n.body_area,
        "suspectedType": n.suspected_type, "suspectedSeverity": n.suspected_severity,
        "trainingImpact": n.training_impact, "dateReported": n.date_reported,
        "expectedClearDate": n.expected_clear_date, "status": n.status,
        "lastCheckInAt": n.last_check_in_at, "resolvedAt": n.resolved_at,
        "relatedNoteId": n.related_note_id, "notes": n.notes,
    }


def log_health_note(db, category, suspected_type=None, suspected_severity=None,
                     training_impact=None, expected_clear_date=None, notes=None,
                     body_area=None, related_note_id=None, user_id: str = DEFAULT_USER_ID,
                     is_test: bool = False) -> dict:
    if category not in VALID_HEALTH_CATEGORIES:
        raise ValueError(f"category must be one of {VALID_HEALTH_CATEGORIES}")
    if body_area is not None:
        if category != "injury":
            raise ValueError("body_area is only valid when category == 'injury'")
        if body_area not in VALID_BODY_AREAS:
            raise ValueError(f"body_area must be one of {VALID_BODY_AREAS}")
    if suspected_severity is not None and suspected_severity not in VALID_SEVERITIES:
        raise ValueError(f"suspected_severity must be one of {VALID_SEVERITIES}")
    if related_note_id is not None and category != "injury":
        raise ValueError("related_note_id is only valid when category == 'injury'")

    note = HealthNote(
        id=f"health_{uuid.uuid4().hex[:12]}", user_id=user_id, category=category,
        body_area=body_area, suspected_type=suspected_type, suspected_severity=suspected_severity,
        training_impact=training_impact, date_reported=local_today(user_id).isoformat(),
        expected_clear_date=expected_clear_date, status="active", related_note_id=related_note_id,
        notes=notes, created_at=datetime.now(timezone.utc).isoformat(), is_test=is_test,
    )
    db.add(note)
    db.commit()
    return _health_note_to_dict(note)


def update_health_status(db, note_id: str, status: str, notes=None, user_id: str = DEFAULT_USER_ID) -> dict:
    if status not in VALID_HEALTH_STATUSES:
        raise ValueError(f"status must be one of {VALID_HEALTH_STATUSES}")
    note = db.get(HealthNote, note_id)
    if not note:
        raise ValueError(f"no health note with id {note_id}")
    note.status = status
    if notes is not None:
        note.notes = notes
    if status == "resolved":
        note.resolved_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    return _health_note_to_dict(note)


def list_health_notes(db, status=None, category=None, user_id: str = DEFAULT_USER_ID) -> list:
    q = db.query(HealthNote).filter(owned_by(HealthNote.user_id, user_id), HealthNote.is_test.isnot(True))
    if status:
        q = q.filter(HealthNote.status == status)
    if category:
        q = q.filter(HealthNote.category == category)
    return [_health_note_to_dict(r) for r in q.order_by(HealthNote.created_at.desc()).all()]


def _workout_to_dict(w: Workout) -> dict:
    return {
        "id": w.id, "scheduledDate": w.scheduled_date, "workoutType": w.workout_type,
        "activityType": w.activity_type, "targetDistanceMi": w.target_distance_mi,
        "targetPaceSecPerMi": w.target_pace_sec_per_mi, "targetDurationSec": w.target_duration_sec,
        "targetHrBpm": w.target_hr_bpm,
        "notes": w.notes, "steps": _steps_from_json(w.steps_json), "status": w.status,
        "linkedRunId": w.linked_run_id, "critiqueText": w.critique_text, "createdAt": w.created_at,
        "source": w.source or "coach",  # legacy-NULL rows predate this column
        "scheduledTime": w.scheduled_time,
    }


# rest/cross_train/strength days aren't a run by default — defaulting them to "Run" let
# any incidental run synced that same day auto-complete a session that was never a run
# (see _find_and_link_workout_run). "Other" normalizes (stats._normalize_activity_type)
# to a bucket real Strava/Garmin activities essentially never land in, so these simply
# stay "planned" until an explicit activityType is given or record_workout_completion
# is called directly.
_NON_RUN_WORKOUT_TYPES = ("rest", "cross_train", "strength")


def _default_activity_type(workout_type: str) -> str:
    return "Other" if workout_type in _NON_RUN_WORKOUT_TYPES else "Run"


def sync_garmin_suggested_workouts(db, entries: list, user_id: str = DEFAULT_USER_ID) -> int:
    """Upserts Garmin adaptive-training-plan suggestions as source="garmin" Workout rows,
    one per (scheduled_date, source) so these never collide with a Coach-scheduled
    workout for the same day. Each entry: {scheduledDate, workoutType, activityType,
    targetDistanceMi?, targetDurationSec?, notes?, garminWorkoutUuid}.

    Garmin can revise its suggestion for a date right up until the workout starts —
    the whole reason this exists is to catch that early and make the change visible
    rather than silently losing the original. On a genuine revision (garmin_workout_uuid
    differs from what's stored), the new suggestion becomes current but the old one is
    preserved as a change note prepended to notes, not discarded. A workout the user has
    already completed is left alone entirely — a later Garmin revision must never
    retroactively rewrite what was actually prescribed/done at the time."""
    synced = 0
    for e in entries:
        existing = (
            db.query(Workout)
            .filter(Workout.scheduled_date == e["scheduledDate"], Workout.source == "garmin",
                    owned_by(Workout.user_id, user_id))
            .first()
        )
        if existing and existing.status != "planned":
            continue  # already completed/skipped — immutable, don't rewrite history
        if existing and existing.garmin_workout_uuid == e.get("garminWorkoutUuid"):
            continue  # unchanged since last sync
        if existing:
            change_note = (
                f"[Garmin revised this suggestion on {local_today(user_id).isoformat()} — was: "
                f"{existing.workout_type}"
                f"{f', {existing.target_distance_mi}mi' if existing.target_distance_mi else ''}"
                f"{f', {round(existing.target_duration_sec / 60)}min' if existing.target_duration_sec else ''}]"
            )
            existing.notes = "\n".join(filter(None, [e.get("notes"), change_note, existing.notes]))
            existing.workout_type = e["workoutType"]
            existing.activity_type = e["activityType"]
            existing.target_distance_mi = e.get("targetDistanceMi")
            existing.target_duration_sec = e.get("targetDurationSec")
            existing.target_hr_bpm = e.get("targetHrBpm")
            existing.garmin_workout_uuid = e.get("garminWorkoutUuid")
        else:
            db.add(Workout(
                id=f"workout_{uuid.uuid4().hex[:12]}", user_id=user_id,
                scheduled_date=e["scheduledDate"], workout_type=e["workoutType"],
                activity_type=e["activityType"], target_distance_mi=e.get("targetDistanceMi"),
                target_duration_sec=e.get("targetDurationSec"), target_hr_bpm=e.get("targetHrBpm"),
                notes=e.get("notes"), status="planned", source="garmin",
                garmin_workout_uuid=e.get("garminWorkoutUuid"),
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
        synced += 1
    db.commit()
    return synced


def create_workout(db, scheduled_date, workout_type, activity_type=None, target_distance_mi=None,
                    target_pace_sec_per_mi=None, target_duration_sec=None, notes=None, steps=None,
                    user_id: str = DEFAULT_USER_ID, source: str = "coach", scheduled_time=None,
                    is_test: bool = False) -> dict:
    """`source`/`scheduled_time` default to the manual/chat-scheduled case — the
    Phase 4.3 generator is the only other caller that passes `source="generator"`
    (and `scheduled_time`, for the 2nd session of a two-a-day), reusing this same
    validation path rather than constructing Workout rows itself. `is_test` (Phase
    12.1) is set only by a chat session tagged with the X-Hale-Test header — never by
    the generator or a real manual/API create."""
    if workout_type not in VALID_WORKOUT_TYPES:
        raise ValueError(f"workout_type must be one of {VALID_WORKOUT_TYPES}")
    cleaned_steps = _validate_steps(steps)
    target_duration_sec = _reconcile_target_duration(cleaned_steps, target_duration_sec)
    workout = Workout(
        id=f"workout_{uuid.uuid4().hex[:12]}", user_id=user_id, scheduled_date=scheduled_date,
        workout_type=workout_type, activity_type=activity_type or _default_activity_type(workout_type),
        target_distance_mi=target_distance_mi, target_pace_sec_per_mi=target_pace_sec_per_mi,
        target_duration_sec=target_duration_sec, notes=notes,
        steps_json=json.dumps(cleaned_steps) if cleaned_steps else None, status="planned",
        created_at=datetime.now(timezone.utc).isoformat(), source=source, scheduled_time=scheduled_time,
        is_test=is_test,
    )
    db.add(workout)
    db.commit()
    return _workout_to_dict(workout)


def update_workout(db, workout_id: str, user_id: str = DEFAULT_USER_ID, **fields) -> dict:
    workout = db.get(Workout, workout_id)
    if not workout:
        raise ValueError(f"no workout with id {workout_id}")
    if "status" in fields and fields["status"] is not None and fields["status"] not in VALID_WORKOUT_STATUSES:
        raise ValueError(f"status must be one of {VALID_WORKOUT_STATUSES}")
    if "workout_type" in fields and fields["workout_type"] is not None and fields["workout_type"] not in VALID_WORKOUT_TYPES:
        raise ValueError(f"workout_type must be one of {VALID_WORKOUT_TYPES}")
    if "steps" in fields and fields["steps"] is not None:
        fields["steps_json"] = json.dumps(_validate_steps(fields["steps"])) or None
    if "steps_json" in fields or "target_duration_sec" in fields:
        final_steps = _steps_from_json(fields.get("steps_json", workout.steps_json))
        final_target = fields["target_duration_sec"] if fields.get("target_duration_sec") is not None else workout.target_duration_sec
        fields["target_duration_sec"] = _reconcile_target_duration(final_steps, final_target)
    became_completed = fields.get("status") == "completed" and workout.status != "completed"
    for key in ("scheduled_date", "workout_type", "activity_type", "target_distance_mi",
                "target_pace_sec_per_mi", "target_duration_sec", "notes", "steps_json", "status",
                "scheduled_time"):
        if key in fields and fields[key] is not None:
            setattr(workout, key, fields[key])
    db.commit()
    if became_completed and workout.workout_type == "strength":
        # Lazy import — generator.py imports coach.py itself (for _validate_steps via
        # create_workout/get_exercise_progress), so importing it back here at module
        # load time would be circular. Deferred to call time, same convention
        # main.py already uses for its own optional/heavy subsystem imports.
        from . import generator
        generator.apply_strength_progression(db, workout)
    return _workout_to_dict(workout)


def delete_workout(db, workout_id: str, user_id: str = DEFAULT_USER_ID):
    workout = db.get(Workout, workout_id)
    if workout:
        db.delete(workout)
        db.commit()


def _training_config_to_dict(c: UserTrainingConfig) -> dict:
    return {
        "maxHr": c.max_hr, "thresholdHr": c.threshold_hr, "ftpWatts": c.ftp_watts,
        "zones": json.loads(c.zones_json) if c.zones_json else None,
        "weeklyRampPct": c.weekly_ramp_pct, "mesocyclePattern": c.mesocycle_pattern,
        "distribution": c.distribution, "strengthDaysPerWeek": c.strength_days_per_week,
        "strengthTemplate": c.strength_template,
        # `or 0` for the legacy-NULL case: rows predating this column read NULL after
        # ALTER TABLE ADD COLUMN (no DB-level default is set — see _migrate_add_missing_
        # columns), and the wire contract is a number, not null.
        "rideDaysPerWeek": c.ride_days_per_week or 0,
        "seedWeeklyMiles": json.loads(c.seed_weekly_miles_json) if c.seed_weekly_miles_json else None,
    }


def get_training_config(db, user_id: str = DEFAULT_USER_ID) -> dict:
    """Returns the user's row, or the schema's own column defaults if they've never
    saved one — a fresh account should see sensible defaults, not a 404. Defaults are
    passed explicitly here rather than relying on the model's Column(default=...):
    those only apply at INSERT/flush time, not to a plain unflushed Python object, so
    a throwaway `UserTrainingConfig(user_id=user_id)` alone would come back with every
    default column still None."""
    config = db.get(UserTrainingConfig, user_id)
    if not config:
        # Phase 28 — sex-aware default (womens_at_home for a female user, matching
        # generator.py's own _default_strength_template) so the nightly automatic
        # rotation starts on the tailored template too, not just Quick Generate.
        user = db.get(User, user_id)
        default_template = "womens_at_home" if user and user.sex == "female" else "full_body_ab"
        config = UserTrainingConfig(
            user_id=user_id, weekly_ramp_pct=3.0, mesocycle_pattern="3:1",
            distribution="pyramidal", strength_days_per_week=2, strength_template=default_template,
            ride_days_per_week=0,
        )
    return _training_config_to_dict(config)


def update_training_config(db, user_id: str = DEFAULT_USER_ID, **fields) -> dict:
    if "mesocycle_pattern" in fields and fields["mesocycle_pattern"] is not None \
            and fields["mesocycle_pattern"] not in VALID_MESOCYCLE_PATTERNS:
        raise ValueError(f"mesocycle_pattern must be one of {VALID_MESOCYCLE_PATTERNS}")
    if "distribution" in fields and fields["distribution"] is not None \
            and fields["distribution"] not in VALID_DISTRIBUTIONS:
        raise ValueError(f"distribution must be one of {VALID_DISTRIBUTIONS}")
    config = db.get(UserTrainingConfig, user_id)
    if not config:
        config = UserTrainingConfig(user_id=user_id)
        db.add(config)
    for key in ("max_hr", "threshold_hr", "ftp_watts", "zones_json", "weekly_ramp_pct",
                "mesocycle_pattern", "distribution", "strength_days_per_week", "strength_template"):
        if key in fields and fields[key] is not None:
            setattr(config, key, fields[key])
    db.commit()
    return _training_config_to_dict(config)


def _exercise_progress_to_dict(p: ExerciseProgress) -> dict:
    return {
        "exercise": p.exercise, "currentWeightLb": p.current_weight_lb,
        "currentRepsTarget": p.current_reps_target, "currentHoldSec": p.current_hold_sec,
        "lastCompletedAt": p.last_completed_at,
    }


def get_exercise_progress(db, exercise: str, user_id: str = DEFAULT_USER_ID) -> dict:
    """Phase 4.4 — returns the exercise's current progression state, or the schema's
    default starting point (8-rep target, no weight/hold set yet) if this exercise
    has never been prescribed before. Same "pass defaults explicitly rather than rely
    on Column(default=...) firing on an unflushed object" fix as get_training_config."""
    progress = db.get(ExerciseProgress, (user_id, exercise))
    if not progress:
        progress = ExerciseProgress(user_id=user_id, exercise=exercise, current_reps_target=8)
    return _exercise_progress_to_dict(progress)


def list_exercise_progress(db, user_id: str = DEFAULT_USER_ID) -> list:
    rows = db.query(ExerciseProgress).filter(owned_by(ExerciseProgress.user_id, user_id)).all()
    return [_exercise_progress_to_dict(r) for r in rows]


def upsert_exercise_progress(db, exercise: str, user_id: str = DEFAULT_USER_ID, **fields) -> dict:
    """Called by generator.py's double-progression rule once a completed session's
    actuals are logged — never directly by a chat tool or REST endpoint (this is
    derived state, not something a user or the model sets by hand)."""
    progress = db.get(ExerciseProgress, (user_id, exercise))
    if not progress:
        progress = ExerciseProgress(user_id=user_id, exercise=exercise, current_reps_target=8)
        db.add(progress)
    for key in ("current_weight_lb", "current_reps_target", "current_hold_sec", "last_completed_at"):
        if key in fields and fields[key] is not None:
            setattr(progress, key, fields[key])
    db.commit()
    return _exercise_progress_to_dict(progress)


def record_workout_completion(db, workout_id: str, run_id=None, critique_text=None,
                               user_id: str = DEFAULT_USER_ID) -> dict:
    workout = db.get(Workout, workout_id)
    if not workout:
        raise ValueError(f"no workout with id {workout_id}")
    if run_id:
        workout.linked_run_id = run_id
    if critique_text is not None:
        workout.critique_text = critique_text
    workout.status = "completed"
    db.commit()
    return _workout_to_dict(workout)


def _find_and_link_workout_run(db, workout: Workout, user_id: str = DEFAULT_USER_ID):
    """Auto-links a planned Workout to the real synced Run that satisfies it — directly
    mirrors stats._find_and_link_race_run, the one existing precedent for this kind of
    matching in this codebase. Strict on activity type (normalized via
    stats._normalize_activity_type, so Garmin's lowercase "running" still matches a
    "Run"-typed workout) — a run workout never links to a Ride and vice versa.
    Strict on date too (exact scheduled_date match only) — a real bug this used to
    have: a +/-1 day tolerance window meant a not-yet-attempted "today" workout
    could claim *yesterday's* already-logged run the moment it became eligible for
    linking, since list_workouts() tries to link any planned workout whose date is
    today-or-earlier on every single read. Caught live: a real evening run got
    claimed by two separate next-day workouts before either had a real run of
    their own to match. Permissive on distance/duration within that one day:
    prefers the closest match among same-type, not-already-claimed candidates,
    with no hard rejection threshold — a run that was cut short still links to
    its planned session rather than being ignored, same principle as the
    race-goal matcher tolerating an imperfect match."""
    if workout.linked_run_id:
        return db.get(Run, workout.linked_run_id)
    try:
        target = datetime.strptime(workout.scheduled_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

    wanted = stats._normalize_activity_type(workout.activity_type)
    already_claimed = {
        row[0] for row in db.query(Workout.linked_run_id).filter(Workout.linked_run_id.isnot(None)).all()
    }

    candidates = db.query(Run).filter(Run.date == target.isoformat()).filter(owned_by(Run.user_id, user_id)).all()
    candidates = [
        r for r in candidates
        if stats._normalize_activity_type(r.activity_type) == wanted and r.id not in already_claimed
    ]
    if not candidates:
        return None

    if workout.target_distance_mi:
        best = min(candidates, key=lambda r: abs((r.distance_mi or 0) - workout.target_distance_mi))
    elif workout.target_duration_sec:
        best = min(candidates, key=lambda r: abs((r.moving_time_sec or 0) - workout.target_duration_sec))
    else:
        best = max(candidates, key=lambda r: r.distance_mi or 0)

    workout.linked_run_id = best.id
    workout.status = "completed"
    db.commit()
    return best


def list_workouts(db, start_date=None, end_date=None, status=None, user_id: str = DEFAULT_USER_ID) -> list:
    q = db.query(Workout).filter(owned_by(Workout.user_id, user_id), Workout.is_test.isnot(True))
    if start_date:
        q = q.filter(Workout.scheduled_date >= start_date)
    if end_date:
        q = q.filter(Workout.scheduled_date <= end_date)
    if status:
        q = q.filter(Workout.status == status)
    workouts = q.order_by(Workout.scheduled_date).all()

    # Auto-link any planned workout whose date has passed against real synced runs —
    # read-time reconciliation, mirrors how stats._find_and_link_race_run works, so no
    # sync-pipeline hook is needed in strava.py/garmin_sync.py for this to happen. This
    # can flip a row's status out from under the `status` filter applied above (a
    # `status=planned` query can trigger a row to become "completed" mid-call) — re-
    # apply the filter after linking so the response always matches what was asked for.
    today = local_today(user_id).isoformat()
    for w in workouts:
        if w.status == "planned" and w.scheduled_date <= today and not w.linked_run_id:
            _find_and_link_workout_run(db, w, user_id)
    if status:
        workouts = [w for w in workouts if w.status == status]

    # Garmin's adaptive plan prescribes HR + duration with distance left null, so a
    # planned session otherwise shows no distance at all. Estimated at read time, not
    # stored: the estimate depends on recent fitness (and, once wired, the forecast), so
    # a value frozen at sync time would be stale by the time the session comes around.
    # Only ever added to rows that genuinely lack a real distance — a prescribed distance
    # always wins over an estimate.
    out = []
    for w in workouts:
        d = _workout_to_dict(w)
        if d["targetDistanceMi"] is None and w.target_hr_bpm and w.target_duration_sec:
            est = stats.estimate_workout_distance(
                db, w.target_hr_bpm, w.target_duration_sec,
                activity_type=w.activity_type or "Run", user_id=user_id,
                workout_type=w.workout_type)
            if est:
                d["estimatedDistance"] = est
        out.append(d)
    return out


# ---------- Recovery tools/sessions (compression boots, etc — see RecoveryTool's
# docstring in models.py for the "why a new table, not folded into Workout" reasoning) ----------

def _recovery_tool_to_dict(t: RecoveryTool) -> dict:
    return {
        "id": t.id, "name": t.name, "category": t.category,
        "minLevel": t.min_level, "maxLevel": t.max_level,
        "minDurationMin": t.min_duration_min, "maxDurationMin": t.max_duration_min,
        "durationIncrementMin": t.duration_increment_min,
        "supportsZoneBoost": bool(t.supports_zone_boost), "notes": t.notes,
    }


def list_recovery_tools(db, user_id: str = DEFAULT_USER_ID) -> list:
    tools = db.query(RecoveryTool).filter(owned_by(RecoveryTool.user_id, user_id)).all()
    return [_recovery_tool_to_dict(t) for t in tools]


def create_recovery_tool(db, name, category, min_level=1, max_level=7, min_duration_min=15,
                          max_duration_min=60, duration_increment_min=15, supports_zone_boost=False,
                          notes=None, user_id: str = DEFAULT_USER_ID) -> dict:
    """Not exposed as a chat tool yet — self-service creation via conversation is the
    planned follow-up mentioned in RecoveryTool's docstring. Exists now so the startup
    seed (models._seed_default_recovery_tool) and that later feature share one
    validated path, same discipline as every other write in this module."""
    if category not in VALID_RECOVERY_CATEGORIES:
        raise ValueError(f"category must be one of {VALID_RECOVERY_CATEGORIES}")
    if min_level > max_level:
        raise ValueError("min_level must be <= max_level")
    if min_duration_min > max_duration_min:
        raise ValueError("min_duration_min must be <= max_duration_min")
    tool_row = RecoveryTool(
        id=f"recoverytool_{uuid.uuid4().hex[:12]}", user_id=user_id, name=name, category=category,
        min_level=min_level, max_level=max_level, min_duration_min=min_duration_min,
        max_duration_min=max_duration_min, duration_increment_min=duration_increment_min,
        supports_zone_boost=supports_zone_boost, notes=notes,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(tool_row)
    db.commit()
    return _recovery_tool_to_dict(tool_row)


def _recovery_session_to_dict(s: RecoverySession) -> dict:
    return {
        "id": s.id, "toolId": s.tool_id, "scheduledDate": s.scheduled_date,
        "level": s.level, "durationMin": s.duration_min, "zoneBoost": bool(s.zone_boost),
        "rationale": s.rationale, "status": s.status, "createdAt": s.created_at,
    }


def recommend_recovery_session(db, tool_id, scheduled_date, level, duration_min, zone_boost=False,
                                rationale=None, user_id: str = DEFAULT_USER_ID) -> dict:
    tool_row = db.get(RecoveryTool, tool_id)
    if not tool_row:
        raise ValueError(f"no recovery tool with id {tool_id}")
    if not (tool_row.min_level <= level <= tool_row.max_level):
        raise ValueError(f"level must be between {tool_row.min_level} and {tool_row.max_level} for {tool_row.name}")
    if not (tool_row.min_duration_min <= duration_min <= tool_row.max_duration_min):
        raise ValueError(
            f"duration_min must be between {tool_row.min_duration_min} and "
            f"{tool_row.max_duration_min} for {tool_row.name}"
        )
    if (duration_min - tool_row.min_duration_min) % tool_row.duration_increment_min != 0:
        raise ValueError(
            f"duration_min must be in {tool_row.duration_increment_min}-minute increments "
            f"from {tool_row.min_duration_min} for {tool_row.name}"
        )
    if zone_boost and not tool_row.supports_zone_boost:
        raise ValueError(f"{tool_row.name} doesn't support zone boost")
    session = RecoverySession(
        id=f"recovery_{uuid.uuid4().hex[:12]}", user_id=user_id, tool_id=tool_id,
        scheduled_date=scheduled_date, level=level, duration_min=duration_min,
        zone_boost=zone_boost, rationale=rationale, status="planned",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(session)
    db.commit()
    return _recovery_session_to_dict(session)


def list_recovery_sessions(db, start_date=None, end_date=None, status=None, user_id: str = DEFAULT_USER_ID) -> list:
    q = db.query(RecoverySession).filter(owned_by(RecoverySession.user_id, user_id))
    if start_date:
        q = q.filter(RecoverySession.scheduled_date >= start_date)
    if end_date:
        q = q.filter(RecoverySession.scheduled_date <= end_date)
    if status:
        q = q.filter(RecoverySession.status == status)
    return [_recovery_session_to_dict(s) for s in q.order_by(RecoverySession.scheduled_date).all()]


def update_recovery_session_status(db, session_id: str, status: str, user_id: str = DEFAULT_USER_ID) -> dict:
    if status not in VALID_RECOVERY_SESSION_STATUSES:
        raise ValueError(f"status must be one of {VALID_RECOVERY_SESSION_STATUSES}")
    session = db.get(RecoverySession, session_id)
    if not session:
        raise ValueError(f"no recovery session with id {session_id}")
    session.status = status
    db.commit()
    return _recovery_session_to_dict(session)


def delete_recovery_session(db, session_id: str, user_id: str = DEFAULT_USER_ID):
    session = db.get(RecoverySession, session_id)
    if session:
        db.delete(session)
        db.commit()
