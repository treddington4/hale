"""Regression tests for the P7a single-table-inheritance discriminator.

These exist because a NULL `sti_type` took production down silently: P7a made `Run`
a polymorphic subclass, which makes SQLAlchemy append `WHERE sti_type = 'run'` to
every `db.query(Run)`/`db.get(Run, ...)` in the codebase. Rows predating the column
kept NULL (ALTER TABLE ADD COLUMN never backfills), so all 554 production rows went
invisible: `GET /api/activities` returned `[]` against a fully-populated table, the
sync path tried to re-INSERT existing rows (`UNIQUE constraint failed: runs.id`), and
pipeline.py's PMC recompute read zero rows and early-returned.

The failure mode is nasty specifically because it is *silent* — a subclass query with
no matching discriminator returns an empty list rather than raising — so it survived a
green test suite, a passing type-check, and a screenshot review. Hence these tests
assert the invariant directly rather than trusting any caller to notice.
"""
import sqlalchemy as sa

from app.models import Activity, Run, STI_TYPE_RUN, _migrate_backfill_sti_type


def _null_out_discriminator(db):
    """Simulate a pre-P7a row: the column exists but was never populated, exactly what
    ALTER TABLE ADD COLUMN leaves behind. Written via raw SQL because the ORM will not
    produce this state itself (the column default fills it in on insert)."""
    db.execute(sa.text("UPDATE runs SET sti_type = NULL"))
    db.commit()
    db.expunge_all()


def test_null_discriminator_makes_rows_invisible_to_orm(db, make_activity):
    """Pins the actual failure, so the migration below is demonstrably load-bearing.
    A regression test that was never proven to fail isn't a safety net."""
    make_activity("strava_1", "Run")
    _null_out_discriminator(db)

    assert db.execute(sa.text("SELECT COUNT(*) FROM runs")).scalar() == 1
    assert db.query(Run).all() == []          # silently empty — the dangerous part
    assert db.get(Run, "strava_1") is None


def test_migration_restores_visibility(db, make_activity):
    make_activity("strava_1", "Run")
    _null_out_discriminator(db)

    _migrate_backfill_sti_type()
    db.expunge_all()

    assert len(db.query(Run).all()) == 1
    assert db.get(Run, "strava_1") is not None
    assert len(db.query(Activity).all()) == 1


def test_migration_is_idempotent(db, make_activity):
    """Runs on every init_db(), so a second pass must be a no-op rather than
    rewriting rows that already carry a discriminator."""
    make_activity("strava_1", "Run")
    _null_out_discriminator(db)

    _migrate_backfill_sti_type()
    _migrate_backfill_sti_type()
    db.expunge_all()

    assert len(db.query(Run).all()) == 1


def test_non_run_activities_also_get_run_discriminator(db, make_activity):
    """The counterintuitive core of the fix. `Run` is the only concrete subclass and
    every sync path constructs `Run(...)` regardless of the real activity type, so a
    stored ride must carry sti_type='run' to stay reachable via `db.get(Run, ...)`.
    Backfilling the real family ('ride') would make the sync miss the row and attempt
    a duplicate INSERT — the original production crash. If a future commit adds real
    Ride/Walk subclasses, this test should fail loudly and be updated alongside the
    write paths, not deleted."""
    make_activity("garmin_1", "Ride", distance_mi=12.0)
    make_activity("hevy_1", "WeightTraining", distance_mi=None, moving_time_sec=3600)
    _null_out_discriminator(db)

    _migrate_backfill_sti_type()
    db.expunge_all()

    assert db.get(Run, "garmin_1") is not None
    assert db.get(Run, "hevy_1") is not None
    stored = {r[0] for r in db.execute(sa.text("SELECT DISTINCT sti_type FROM runs"))}
    assert stored == {STI_TYPE_RUN}


def test_orm_inserts_always_populate_the_discriminator(db, make_activity):
    """Guards the other half of the invariant: if a write path ever stopped setting a
    discriminator, the migration would just be papering over a fresh NULL every start."""
    make_activity("garmin_1", "Ride", distance_mi=12.0)

    assert db.execute(sa.text("SELECT COUNT(*) FROM runs WHERE sti_type IS NULL")).scalar() == 0
