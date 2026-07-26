"""Backend port of web/src/lib/runs.ts's mergeDuplicateRuns/isLikelyDuplicate/
mergeRunGroup/canonicalActivityType — see CLAUDE.md's "Two independent sync sources"
section for why duplicates exist in storage in the first place (Strava + Garmin each
own their own fetch->normalize->upsert pipeline, never deduplicated at write time).

This is a faithful port, not a reinterpretation: same transitive-closure grouping
(re-scan to a fixed point, not a single pairwise pass), same source-preference merge
order (Strava > Hevy > first-seen, with Hevy/Garmin name and Garmin-route exceptions),
and same same-source corroboration rule (two rows sharing a source only join a group
when a differently-sourced member already in the group vouches for both). Keep this in
sync with runs.ts by hand if the merge heuristic ever changes there — same discipline
as GAP's backend/frontend duplication (see CLAUDE.md).

Operates on ORM `Run` rows rather than the frontend's camelCase API shape, so
"emptiness" for backfill purposes means None or an empty-JSON-array string ("[]"), not
an empty parsed list — the Text columns (splits_json, route_json, etc.) are still raw
JSON strings at this layer, never decoded.
"""
from .models import Run
from .stats import _normalize_activity_type

_RUN_COLUMNS = Run.__table__.columns.keys()


def _is_empty_value(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v == "" or v == "[]"
    return False


def _to_minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _is_likely_duplicate(a: Run, b: Run) -> bool:
    """Pure date/type/distance/time proximity check — deliberately does not check
    source distinctness itself; that's the caller's job (see merge_duplicate_runs'
    same-source corroboration rule)."""
    if a.date != b.date:
        return False

    type_a = _normalize_activity_type(a.activity_type)
    type_b = _normalize_activity_type(b.activity_type)

    # Strava's own generic "Workout" activity_type doesn't meaningfully discriminate
    # between families (confirmed live: Hevy's direct Strava-posting integration uses
    # it for some workouts instead of "WeightTraining") — treated as a wildcard here,
    # not by widening _normalize_activity_type itself, which stays a general-purpose
    # classifier used elsewhere.
    def is_wildcard(t: str) -> bool:
        return t == "workout"

    if type_a != type_b and not is_wildcard(type_a) and not is_wildcard(type_b):
        return False
    effective_type = type_b if is_wildcard(type_a) else type_a

    # Strength activities carry no meaningful distance (Garmin/Hevy both report 0 or
    # null) — match on start-time proximity alone instead, same ~30min tolerance the
    # backend's own Hevy<->Garmin matcher uses elsewhere for this exact pairing.
    if effective_type == "strength":
        if not a.start_time or not b.start_time:
            return False
        return abs(_to_minutes(a.start_time) - _to_minutes(b.start_time)) <= 30

    if a.distance_mi is None or b.distance_mi is None:
        return False
    if abs(a.distance_mi - b.distance_mi) > max(0.1, a.distance_mi * 0.05):
        return False
    if a.start_time and b.start_time:
        if abs(_to_minutes(a.start_time) - _to_minutes(b.start_time)) > 10:
            return False
    return True


class _MergedActivity:
    """Read-only view over a group of duplicate Run rows. Exposes the same attributes
    a plain Run ORM instance would (falling back to the chosen primary row for any
    column not overridden by the merge), so every existing stats.py consumer that reads
    r.distance_mi / r.moving_time_sec / etc. keeps working unmodified."""

    def __init__(self, primary: Run, overrides: dict, merged_sources: list[str], merged_ids: list[str]):
        object.__setattr__(self, "_primary", primary)
        object.__setattr__(self, "_overrides", overrides)
        object.__setattr__(self, "merged_sources", merged_sources)
        object.__setattr__(self, "merged_ids", merged_ids)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_primary"), name)


def _merge_run_group(group: list[Run]) -> _MergedActivity:
    """Takes a group of 2+ mutual duplicates (not just a pair) — a real account can
    have a Garmin activity auto-forwarded to Strava on top of a Hevy log of the same
    workout, producing three-plus raw rows for one physical session."""
    strava = next((r for r in group if r.source == "strava"), None)
    hevy = next((r for r in group if r.source == "hevy"), None)
    garmin = next((r for r in group if r.source == "garmin"), None)
    primary = strava or hevy or group[0]

    # Start from primary's own fields (never overwritten below), then let the rest of
    # the group backfill anything primary doesn't have.
    overrides: dict = {}
    for r in group:
        if r is primary:
            continue
        for col in _RUN_COLUMNS:
            current = overrides[col] if col in overrides else getattr(primary, col)
            if _is_empty_value(current):
                val = getattr(r, col)
                if not _is_empty_value(val):
                    overrides[col] = val

    # Hevy's manually-logged exercise/set data is authoritative over Garmin's own
    # auto-detection; Garmin's activity names are usually more descriptive than
    # Strava's generic auto-names. Both override the general primary-based merge
    # above regardless of merge order.
    if hevy is not None:
        if not _is_empty_value(hevy.name):
            overrides["name"] = hevy.name
        if not _is_empty_value(hevy.exercise_sets_json):
            overrides["exercise_sets_json"] = hevy.exercise_sets_json
    elif garmin is not None and not _is_empty_value(garmin.name):
        overrides["name"] = garmin.name

    # Garmin's route is parsed from the raw FIT record stream, not subject to
    # whatever privacy zone Strava applies — overrides the general primary-based merge
    # whenever Garmin has real route data to offer.
    if garmin is not None and not _is_empty_value(garmin.route_json):
        overrides["route_json"] = garmin.route_json
        overrides["route_metrics_json"] = garmin.route_metrics_json

    merged_sources = sorted({r.source for r in group})
    merged_ids = [r.id for r in group]
    return _MergedActivity(primary, overrides, merged_sources, merged_ids)


def merge_duplicate_runs(rows: list[Run]) -> list:
    """Groups+merges duplicate rows via transitive closure, mirroring runs.ts's
    mergeDuplicateRuns exactly (including its same-source corroboration rule)."""
    n = len(rows)
    used = [False] * n
    merged: list = []
    for i in range(n):
        if used[i]:
            continue
        group = [rows[i]]
        used[i] = True
        # Re-scan to a fixed point rather than a single forward pass: a same-source
        # candidate that needs corroboration may sit earlier in the array than the
        # differently-sourced row that would corroborate it.
        added_any = True
        while added_any:
            added_any = False
            for j in range(n):
                if used[j]:
                    continue
                candidate = rows[j]
                same_source_present = any(g.source == candidate.source for g in group)
                if same_source_present:
                    if any(
                        g.source != candidate.source and _is_likely_duplicate(g, candidate)
                        for g in group
                    ):
                        group.append(candidate)
                        used[j] = True
                        added_any = True
                else:
                    if all(_is_likely_duplicate(g, candidate) for g in group):
                        group.append(candidate)
                        used[j] = True
                        added_any = True
        merged.append(_merge_run_group(group) if len(group) > 1 else rows[i])
    return merged
