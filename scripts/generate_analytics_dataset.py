#!/usr/bin/env python3
"""Generate and load the curated dataset into `gold_dev_analytics`.

    PYTHONPATH=src python3 scripts/generate_analytics_dataset.py --report
    PYTHONPATH=src python3 scripts/generate_analytics_dataset.py --dry-run
    PYTHONPATH=src python3 scripts/generate_analytics_dataset.py --prune --load --profile anurag-r

Design notes worth knowing before running this:

**`--prune` and `--load` are separate flags on purpose.** Pruning deletes every row from 18
tables — including `fact_restock_request` and `quote_metadata`, which destroys the existing
review-app demo quotes. It is the point of no return for the replica's current contents, so it
never happens as a side effect of generating or loading. `--load` into un-pruned tables appends,
which is almost never what you want; the script says so rather than guessing.

**The catalog is guarded.** Only `gold_dev_analytics` is a valid target. `gold_dev` is Data
Engineering's and this script must never point at it — see `docs/schema_changes_gold_dev_analytics.md`.

**Nothing loads unless the self-assertions pass.** A subtly wrong dataset is worse than none:
every detector built on it would be graded against wrong answers, and Phase 2's gate would
report a plausible, meaningless number. `--force` exists for debugging and prints what it is
overriding.

Table DDL is never touched: rows are `DELETE`d, never `DROP`ped. The one exception is
`sim_ground_truth`, which this project owns and creates if absent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentic_restock.generation import assertions, dataset, ground_truth

ALLOWED_CATALOG = "gold_dev_analytics"
STAGING_VOLUME = "dataset_staging"
STAGING_SCHEMA = "supply_chain_analytics"

# Ours, so we DROP and recreate it rather than preserving its shape. Everything else is
# DE-shaped and only ever has rows replaced -- but this table's columns change as the estimators
# being graded change, and `CREATE TABLE IF NOT EXISTS` silently keeps an older schema, which
# leaves the new columns absent and the gate grading against nulls.
GROUND_TRUTH_DROP = "DROP TABLE IF EXISTS {catalog}.supply_chain_analytics.sim_ground_truth"

GROUND_TRUTH_DDL = """
CREATE TABLE {catalog}.supply_chain_analytics.sim_ground_truth (
  SUBJECT_TYPE STRING COMMENT 'PAIR or SUPPLIER_PART',
  SUBJECT_ID STRING,
  PART_ID STRING, WAREHOUSE_ID STRING, SUPPLIER_ID STRING,
  REGIME STRING, HISTORY_DAYS INT, HISTORY_COHORT STRING,
  TRUE_LEVEL DOUBLE, TRUE_EFFECTIVE_LEVEL DOUBLE, TRUE_NOISE_CV DOUBLE,
  TRUE_SEASONAL_AMPLITUDE DOUBLE, TRUE_TREND_PER_DAY DOUBLE,
  STEP_DATE_KEY INT, TRUE_STEP_FACTOR DOUBLE,
  TRUE_MEAN_INTERVAL_DAYS DOUBLE, TRUE_MEAN_ISSUE_SIZE DOUBLE, QUIET_SINCE_DATE_KEY INT,
  REALISED_ISSUE_MEAN DOUBLE, REALISED_NONZERO_MEAN DOUBLE,
  REALISED_ISSUE_CV DOUBLE, REALISED_ZERO_DAY_FRACTION DOUBLE,
  RECOVERABLE_DAILY_RATE DOUBLE,
  SAFETY_STOCK_QTY INT, MAX_STOCK_LEVEL INT, CLOSING_QTY INT, IN_TRANSIT_QTY INT,
  UNIT_COST DOUBLE, CRITICALITY_CLASS STRING,
  SUPPLIER_ARCHETYPE STRING, CONTRACTED_LEAD_DAYS INT,
  TRUE_MU_LEAD_DAYS DOUBLE, TRUE_SIGMA_LEAD_DAYS DOUBLE, TRUE_REJECT_RATE DOUBLE,
  CAPTURE_TIER STRING, OBSERVED_DELIVERY_N INT,
  OBSERVED_SUPPLIER_N INT, OBSERVED_CATEGORY_N INT,
  OBSERVED_MEAN_DELAY_DAYS DOUBLE, OBSERVED_SIGMA_DELAY_DAYS DOUBLE, OBSERVED_OTD_RATE DOUBLE,
  EXPECTED_FALLBACK_TIER STRING,
  MOQ INT, PACK_SIZE INT, CONTRACT_UNIT_COST DOUBLE, IS_PREFERRED BOOLEAN,
  PLANTED_FINDING_IDS STRING, EXPECT_FINDING BOOLEAN,
  DW_SOURCE STRING
) COMMENT 'The parameters the curated dataset was generated with, so detector accuracy is measurable rather than reviewable. Written by scripts/generate_analytics_dataset.py; see docs/dataset_generator_spec.md §4.'
"""


def run_sql(sql: str, profile: str, *, quiet: bool = False) -> str:
    result = subprocess.run(
        [
            "databricks",
            "experimental",
            "aitools",
            "tools",
            "query",
            sql,
            "--profile",
            profile,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed:\n{sql[:400]}\n{result.stderr}")
    if not quiet:
        print(f"    ok: {result.stdout.strip()[:120]}")
    return result.stdout


def report(data: dict[str, list[dict]]) -> None:
    counts = dataset.row_counts(data)
    width = max(len(name) for name in counts)
    print("\nRow counts")
    for name, count in counts.items():
        print(f"  {name:<{width}}  {count:>8,}")
    print(f"  {'TOTAL':<{width}}  {sum(counts.values()):>8,}")

    from agentic_restock.generation import replenishment, supplier_facts

    histories = replenishment.simulate_all()
    records = supplier_facts.delivery_records(histories)
    print("\nCoverage")
    print(json.dumps(ground_truth.coverage(histories, records), indent=2))


def verify(*, force: bool) -> bool:
    print("\nSelf-assertions (docs/dataset_generator_spec.md §5)")
    results = assertions.run_all()
    for name, problems in results.items():
        print(f"  {'PASS' if not problems else 'FAIL':<5} {name}")
        for problem in problems:
            print(f"        - {problem}")

    problems = assertions.failures(results)
    if not problems:
        print("  all assertions passed")
        return True
    if force:
        print(f"\n  --force: proceeding despite {len(problems)} failure(s)")
        return True
    print(f"\n  {len(problems)} failure(s). Refusing to load. Use --force to override.")
    return False


def prune(catalog: str, profile: str, only: list[str] | None = None) -> None:
    """Clear the target tables. `only` restricts it to a named subset.

    The subset exists because a full prune is rarely what a fix needs and is never cheap to
    undo: it also clears `quote_metadata`, and with it the summary reports of every quote the
    live pipeline has written. When one generator module changes, regenerating the one table it
    feeds is the whole job -- and leaves the approval queue standing.
    """
    targets = list(only) if only else list(dataset.PRUNE_TARGETS)
    print("\nPruning (DELETE, never DROP — table DDL stays Data Engineering's)")
    for name in targets:
        target = dataset.TARGETS[name]
        fqn = f"{catalog}.{target.schema}.{target.table}"
        try:
            run_sql(f"DELETE FROM {fqn} WHERE TRUE", profile, quiet=True)
            print(f"  cleared {fqn}")
        except RuntimeError as error:
            if "TABLE_OR_VIEW_NOT_FOUND" in str(error):
                print(f"  skipped {fqn} (does not exist yet)")
            else:
                raise
    if only:
        # A partial prune leaves fact_restock_request alone, so its quote ids are still valid
        # and their metadata must not be cleared.
        return

    # Quote metadata is ours and references quote ids that no longer exist after a rebuild.
    try:
        run_sql(
            f"DELETE FROM {catalog}.supply_chain_analytics.quote_metadata WHERE TRUE",
            profile,
            quiet=True,
        )
        print(f"  cleared {catalog}.supply_chain_analytics.quote_metadata")
    except RuntimeError as error:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(error):
            raise


def _write_parquet(rows: list[dict], path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Every key across every row, including the '_'-prefixed load-time helpers: `_request_status`
    # and `_urgency_level` are not table columns but the status-resolving join reads them from
    # the staged file. The INSERT column list is built from the target's own schema, so extras
    # cannot leak into the table.
    columns = dataset.all_keys(rows)
    payload = [{c: row.get(c) for c in columns} for row in rows]

    # A column that is None on every row has no inferable type. pyarrow writes it as a null
    # field, Spark reads that back as INT, and the insert then fails with "cannot cast INT to
    # DATE" -- which an explicit CAST does NOT fix, because Spark rejects INT->DATE outright.
    # Writing such columns as strings makes the cast to any target type legal.
    schema = pa.Table.from_pylist(payload).schema
    adjusted = pa.schema(
        [
            pa.field(field.name, pa.string()) if pa.types.is_null(field.type) else field
            for field in schema
        ]
    )
    pq.write_table(pa.Table.from_pylist(payload, schema=adjusted), path)


def _target_columns(fqn: str, profile: str) -> list[tuple[str, str]]:
    """(name, type) for every column on the target table, in ordinal order."""
    raw = run_sql(
        "SELECT column_name, data_type FROM "
        f"{fqn.split('.')[0]}.information_schema.columns "
        f"WHERE table_schema = '{fqn.split('.')[1]}' AND table_name = '{fqn.split('.')[2]}' "
        "ORDER BY ordinal_position",
        profile,
        quiet=True,
    )
    return [(row["column_name"], row["data_type"]) for row in json.loads(raw)]


def _padded_insert(
    fqn: str,
    generated: list[str],
    profile: str,
    overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the INSERT column list and SELECT list, filling columns we do not generate.

    Delta rejects a partial column list outright (`DELTA_INSERT_COLUMN_MISMATCH`), so every
    column has to be named. The ones we do not generate are the DW_* audit stamps: timestamps
    get `current_timestamp()` because that is what they mean, and anything else gets NULL rather
    than a fabricated value.

    `overrides` supplies an expression for a column sourced from somewhere other than the
    parquet file — currently just `REQUEST_STATUS_KEY`, resolved by joining the real
    `dim_request_status` dimension.
    """
    overrides = overrides or {}
    have = set(generated)
    insert_cols: list[str] = []
    select_exprs: list[str] = []

    for name, data_type in _target_columns(fqn, profile):
        insert_cols.append(f"`{name}`")
        if name in overrides:
            select_exprs.append(overrides[name])
        elif name in have:
            # Cast to the target's own declared type. Parquet's inferred type does not always
            # match: an all-NULL column arrives as INT and the insert fails on
            # DATATYPE_MISMATCH against a DATE target, which is a property of the *data* rather
            # than of the schema and so cannot be fixed once and forgotten.
            select_exprs.append(f"CAST(src.`{name}` AS {data_type})")
        elif data_type.upper().startswith("TIMESTAMP"):
            select_exprs.append("current_timestamp()")
        else:
            select_exprs.append(f"CAST(NULL AS {data_type})")

    return ", ".join(insert_cols), ", ".join(select_exprs)


def load(catalog: str, profile: str, data: dict[str, list[dict]]) -> None:
    volume_root = f"/Volumes/{catalog}/{STAGING_SCHEMA}/{STAGING_VOLUME}"
    print(f"\nLoading via {volume_root}")

    # Only when it is actually being reloaded. It is DROPped rather than DELETEd because its
    # schema tracks whatever the estimators currently report, so a partial load that recreated
    # it would leave an empty table behind and silently lose the grading history.
    if "sim_ground_truth" in data:
        run_sql(GROUND_TRUTH_DROP.format(catalog=catalog), profile, quiet=True)
        run_sql(GROUND_TRUTH_DDL.format(catalog=catalog), profile, quiet=True)
        print("  recreated sim_ground_truth (ours; schema tracks the estimators being graded)")

    with tempfile.TemporaryDirectory() as tmp:
        for name, rows in data.items():
            if not rows:
                print(f"  {name}: no rows, skipped")
                continue
            target = dataset.TARGETS[name]
            fqn = f"{catalog}.{target.schema}.{target.table}"

            local = Path(tmp) / f"{name}.parquet"
            _write_parquet(rows, local)
            remote = f"{volume_root}/{name}.parquet"

            # `databricks fs cp` needs the dbfs: scheme for a Volume destination -- a bare
            # /Volumes/... path is treated as a LOCAL path and fails with a confusing
            # "no such directory: /Volumes/..." error. SQL, in contrast, reads the volume by
            # its bare path, so the two forms genuinely differ and cannot be shared.
            upload = subprocess.run(
                ["databricks", "fs", "cp", "--overwrite", str(local), f"dbfs:{remote}",
                 "--profile", profile],
                capture_output=True, text=True, check=False,
            )
            if upload.returncode != 0:
                raise RuntimeError(f"upload of {name} failed: {upload.stderr}")

            columns = dataset.table_columns(rows)
            if target.resolve_status:
                # dim_request_status is a real 17-row dimension with hash keys and is NOT
                # regenerated, so the status arrives by name and is resolved here.
                insert_cols, select_exprs = _padded_insert(
                    fqn,
                    [c for c in columns if c != "REQUEST_STATUS_KEY"],
                    profile,
                    overrides={"REQUEST_STATUS_KEY": "st.REQUEST_STATUS_KEY"},
                )
                sql = f"""
                INSERT INTO {fqn} ({insert_cols})
                SELECT {select_exprs}
                FROM parquet.`{remote}` src
                JOIN {catalog}.dim.dim_request_status st
                  ON st.REQUEST_STATUS = src.`_request_status`
                 AND st.URGENCY_LEVEL = src.`_urgency_level`
                """
            else:
                insert_cols, select_exprs = _padded_insert(fqn, columns, profile)
                sql = f"""
                INSERT INTO {fqn} ({insert_cols})
                SELECT {select_exprs} FROM parquet.`{remote}` src
                """
            run_sql(sql, profile, quiet=True)
            print(f"  loaded {len(rows):>7,} rows -> {fqn}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=ALLOWED_CATALOG)
    parser.add_argument("--profile", default=None, help="Databricks CLI profile (required to load)")
    parser.add_argument("--report", action="store_true", help="row counts and coverage, no writes")
    parser.add_argument("--dry-run", action="store_true", help="generate and assert, no writes")
    parser.add_argument("--prune", action="store_true", help="DESTRUCTIVE: clear all target tables")
    parser.add_argument("--load", action="store_true", help="write the generated rows")
    parser.add_argument(
        "--only",
        action="append",
        metavar="TABLE",
        help=(
            "restrict --prune/--load to this generated table; repeatable. "
            "Use when one generator module changed, so the rest of the replica -- and the "
            "quote metadata a full prune clears -- is left alone."
        ),
    )
    parser.add_argument("--force", action="store_true", help="load even if assertions fail")
    parser.add_argument(
        "--i-know-this-is-not-the-replica",
        action="store_true",
        help="override the catalog guard. Never use this against gold_dev.",
    )
    args = parser.parse_args()

    if args.catalog != ALLOWED_CATALOG and not args.i_know_this_is_not_the_replica:
        print(
            f"Refusing to target '{args.catalog}'. Only '{ALLOWED_CATALOG}' is a valid target;\n"
            "'gold_dev' belongs to Data Engineering. Override deliberately if you must."
        )
        return 2

    print(f"Generating dataset for {args.catalog}")
    data = dataset.build()
    print(f"  {sum(dataset.row_counts(data).values()):,} rows across {len(data)} tables")

    if args.report:
        report(data)
        return 0

    if not verify(force=args.force):
        return 1

    if args.dry_run or not (args.prune or args.load):
        print("\nNothing written. Pass --prune and/or --load to apply.")
        return 0

    if not args.profile:
        print("\n--profile is required to write. Never let the CLI pick a default.")
        return 2

    if args.load and not args.prune:
        print(
            "\nWARNING: --load without --prune appends to whatever is already there,\n"
            "         which will duplicate keys. Pass --prune to replace instead."
        )

    only = args.only
    if only:
        unknown = [name for name in only if name not in dataset.TARGETS]
        if unknown:
            print(f"\nNot generated tables: {', '.join(unknown)}")
            return 2
        data = {name: rows for name, rows in data.items() if name in only}
        print(f"\nRestricted to: {', '.join(only)}")

    if args.prune:
        prune(args.catalog, args.profile, only)
    if args.load:
        load(args.catalog, args.profile, data)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
