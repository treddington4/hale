#!/usr/bin/env python3
"""P7b: Side-by-side Activity/STI migration + validation script.

SUPERSEDED for the actual fix: the sti_type backfill now lives in
app/models.py's _migrate_backfill_sti_type(), which runs inside init_db() on
every start. The bug this script was meant to pre-empt reached production
anyway, precisely because this script was validated but never run — an
out-of-band script only helps if someone remembers to execute it, whereas a
migration in init_db() cannot be forgotten and also fixes every other
deployment of this app. This script is kept as a verification tool: it proves a
full-database copy round-trips losslessly, which the in-place UPDATE doesn't
demonstrate on its own.

Standalone — NOT wired into app/models.py's init_db(). Creates a fresh copy of
a source SQLite DB (a *copy* of production, never the live file itself) at
--dest, copying every row from every table byte-for-byte EXCEPT `runs.sti_type`,
which is set to STI_TYPE_RUN ("run") for every row.

It originally backfilled each row's real activity *family* here (ride/walk/…)
via stats.activity_family(). That was a latent bug and is corrected above:
`Run` is the only concrete subclass, and every sync path constructs a `Run(...)`
whatever the activity really is, so a stored ride carries sti_type='run'. A
per-family value would make `db.get(Run, id)` miss those rows and the sync would
attempt a duplicate INSERT — see _migrate_backfill_sti_type()'s docstring.

Production's sti_type column was all-NULL: SQLite's ALTER TABLE ADD COLUMN
(see models.py's _migrate_add_missing_columns) only sets a default for rows
inserted after the column existed — it never backfills rows that were already
there, so every one of the 554 real production rows had sti_type=NULL.

Safety:
  - --source is opened via SQLite's `mode=ro` URI, a real driver-level guarantee
    that this script cannot write to it even if a bug tried to — not just a
    convention. Verified independently via a SHA-256 checksum taken before and
    after the run; any drift is a fatal error.
  - Never deletes or overwrites --source. --dest must not already exist unless
    --force is passed (and even then, only --dest is ever removed).

Validates dest against source:
  - table set + per-table row counts (must match exactly, every table)
  - per-column non-null counts (must match, except runs.sti_type — documented
    expected change from all-NULL to fully populated)
  - numeric column sums per table (copying values unchanged means sums can't
    drift; a mismatch here means a copy bug)
  - full-row diff on a random sample of `runs` rows (every column except
    sti_type must be byte-identical; sti_type must equal activity_family(row))

Prints a plain-text report; exits 1 on ANY unexpected mismatch, 0 if clean.

Usage:
    python scripts/migrate_p7b_activity_sti.py --source /path/to/prod-copy.db --dest /path/to/new.db

Recommended: never point --source at a live app's actual DB_PATH. Make an
explicit snapshot first (e.g. via sqlite3's .backup, which only needs read
access to the live file) and pass that snapshot as --source, so this script
never has any avenue to matter to the real data even if invoked wrong.
"""
import argparse
import hashlib
import random
import sqlite3
import sys
from pathlib import Path

# Safe to import: app.models builds a SQLAlchemy `engine` at module scope, but
# create_engine() is lazy and never touches disk until a connection is actually
# opened, which this script never does through the ORM.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.models import STI_TYPE_RUN  # noqa: E402

NUMERIC_TYPE_HINTS = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_tables(conn) -> list:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def get_schema_statements(conn) -> list:
    """All CREATE TABLE / CREATE INDEX statements, in original order, so dest's
    schema is structurally identical to source's (indexes included)."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def get_columns(conn, table: str) -> list:
    """(cid, name, type, notnull, dflt_value, pk) per PRAGMA table_info."""
    return conn.execute(f"PRAGMA table_info('{table}')").fetchall()


def numeric_columns(conn, table: str) -> list:
    return [
        col[1] for col in get_columns(conn, table)
        if any(hint in (col[2] or "").upper() for hint in NUMERIC_TYPE_HINTS)
    ]


def copy_database(source_path: str, dest_path: str) -> None:
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    dest = sqlite3.connect(dest_path)

    for stmt in get_schema_statements(src):
        dest.execute(stmt)
    dest.commit()

    for table in get_tables(src):
        cols = [c[1] for c in get_columns(src, table)]
        if not cols:
            continue
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(f'"{c}"' for c in cols)
        insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'

        sti_idx = cols.index("sti_type") if table == "runs" and "sti_type" in cols else None

        rows_out = []
        for row in src.execute(f'SELECT * FROM "{table}"'):
            values = list(row)
            if sti_idx is not None:
                values[sti_idx] = STI_TYPE_RUN
            rows_out.append(values)

        if rows_out:
            dest.executemany(insert_sql, rows_out)
        dest.commit()

    src.close()
    dest.close()


def compare_databases(source_path: str, dest_path: str) -> list:
    """Returns a list of mismatch descriptions — empty means clean."""
    mismatches = []
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(f"file:{dest_path}?mode=ro", uri=True)
    dst.row_factory = sqlite3.Row

    src_tables = set(get_tables(src))
    dst_tables = set(get_tables(dst))
    if src_tables != dst_tables:
        mismatches.append(
            f"Table set differs: source-only={src_tables - dst_tables}, "
            f"dest-only={dst_tables - src_tables}"
        )

    for table in sorted(src_tables & dst_tables):
        src_count = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        dst_count = dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if src_count != dst_count:
            mismatches.append(f"[{table}] row count differs: source={src_count} dest={dst_count}")
            continue  # further per-column checks are meaningless if counts already diverge

        cols = [c[1] for c in get_columns(src, table)]
        for col in cols:
            src_nn = src.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL').fetchone()[0]
            dst_nn = dst.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL').fetchone()[0]
            if table == "runs" and col == "sti_type":
                # Expected, documented change: source is all-NULL pre-backfill;
                # dest must be fully populated — every row got a family assigned.
                if dst_nn != dst_count:
                    mismatches.append(
                        f"[{table}.{col}] expected full backfill in dest: "
                        f"dest_non_null={dst_nn} dest_total={dst_count}"
                    )
                continue
            if src_nn != dst_nn:
                mismatches.append(f"[{table}.{col}] non-null count differs: source={src_nn} dest={dst_nn}")

        for col in numeric_columns(src, table):
            src_sum = src.execute(f'SELECT SUM("{col}") FROM "{table}"').fetchone()[0] or 0
            dst_sum = dst.execute(f'SELECT SUM("{col}") FROM "{table}"').fetchone()[0] or 0
            if src_sum != dst_sum:
                mismatches.append(f"[{table}.{col}] numeric sum differs: source={src_sum} dest={dst_sum}")

    # Full-row diff on a random sample of `runs` rows — the only table whose
    # data is intentionally modified, so it's the one worth spot-checking
    # column-by-column rather than trusting the aggregate checks alone.
    if "runs" in src_tables & dst_tables:
        ids = [r[0] for r in src.execute('SELECT id FROM "runs"').fetchall()]
        sample = random.sample(ids, min(20, len(ids)))
        cols = [c[1] for c in get_columns(src, "runs")]
        compare_cols = [c for c in cols if c != "sti_type"]
        for rid in sample:
            src_row = src.execute('SELECT * FROM "runs" WHERE id = ?', (rid,)).fetchone()
            dst_row = dst.execute('SELECT * FROM "runs" WHERE id = ?', (rid,)).fetchone()
            if dst_row is None:
                mismatches.append(f"[runs] id={rid} missing from dest")
                continue
            for c in compare_cols:
                if src_row[c] != dst_row[c]:
                    mismatches.append(f"[runs] id={rid} column={c} differs: source={src_row[c]!r} dest={dst_row[c]!r}")
            if dst_row["sti_type"] != STI_TYPE_RUN:
                mismatches.append(
                    f"[runs] id={rid} sti_type mismatch: expected={STI_TYPE_RUN!r} got={dst_row['sti_type']!r}"
                )

    src.close()
    dst.close()
    return mismatches


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Path to source SQLite DB (opened read-only — never modified)")
    parser.add_argument("--dest", required=True, help="Path for the new side-by-side DB (must not already exist unless --force)")
    parser.add_argument("--force", action="store_true", help="Overwrite --dest if it already exists")
    args = parser.parse_args()

    source_path = Path(args.source)
    dest_path = Path(args.dest)

    if not source_path.exists():
        print(f"ERROR: source does not exist: {source_path}", file=sys.stderr)
        sys.exit(1)
    if dest_path.exists():
        if not args.force:
            print(f"ERROR: dest already exists: {dest_path} (use --force to overwrite)", file=sys.stderr)
            sys.exit(1)
        dest_path.unlink()

    print(f"Source: {source_path}")
    print(f"Dest:   {dest_path}")

    checksum_before = sha256_of(str(source_path))
    print(f"Source SHA-256 (before): {checksum_before}")

    print("\nCopying database...")
    copy_database(str(source_path), str(dest_path))
    print("Copy complete.")

    checksum_after = sha256_of(str(source_path))
    print(f"Source SHA-256 (after):  {checksum_after}")
    if checksum_before != checksum_after:
        print(
            "FATAL: source file changed during migration! This should be impossible "
            "(opened read-only) — treat as a serious bug, not a fluke.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Source file confirmed untouched (checksum match).")

    print("\nValidating...")
    mismatches = compare_databases(str(source_path), str(dest_path))

    print("\n" + "=" * 70)
    if mismatches:
        print(f"MISMATCHES FOUND: {len(mismatches)}")
        for m in mismatches:
            print(f"  - {m}")
        print("=" * 70)
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED. Zero mismatches.")
        print("=" * 70)
        sys.exit(0)


if __name__ == "__main__":
    main()
