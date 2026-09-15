"""Scan run log — records that a run happened even when nothing needed action.

Without this, "nothing needed attention" and "the job silently broke" are
indistinguishable from the outside, and there is no way to report the
alert-fatigue counterpoint this product leans on: "we were quiet on 8 of 14
runs this week" (docs/market_evidence_phase1.md §3). One row per scan run,
written whether or not a Supervisor conversation was opened.

`settings_snapshot` holds the **resolved** settings in force for that run — every
key, including the ones that were left at their default. Storing the resolved set
rather than only the overridden rows is what makes a run reproducible from its own
record: "the defaults at the time" is precisely the thing that will have moved by
the time anyone asks why a recommendation looked the way it did.
"""

from agentic_restock.config import qualified_table

TABLE_SCAN_RUN_LOG = "scan_run_log"


def build_run_log_table_ddl(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    table = qualified_table(TABLE_SCAN_RUN_LOG, app_catalog, app_schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      run_at TIMESTAMP COMMENT 'When this scan ran',
      candidate_count INT COMMENT 'Findings kept after suppression this run',
      outcome STRING COMMENT 'NO_ACTION or SUPERVISOR_INVOKED',
      note STRING COMMENT 'Free-text detail, e.g. why the Supervisor call was skipped',
      settings_snapshot STRING COMMENT 'Resolved settings in force for this run, as JSON'
    )
    COMMENT 'One row per scan run, including quiet ones -- see module docstring.'
    """.strip()


def build_run_log_migration(
    app_catalog: str | None = None, app_schema: str | None = None
) -> str:
    """Add `settings_snapshot` to a log table created before settings existed.

    `CREATE TABLE IF NOT EXISTS` will not widen an existing table, so a log table from
    before this column was added needs one ALTER. Safe to run every time -- the caller
    tolerates the "column already exists" failure rather than checking first, because a
    missing audit column must never stop a scan.
    """
    table = qualified_table(TABLE_SCAN_RUN_LOG, app_catalog, app_schema)
    return f"""
    ALTER TABLE {table}
    ADD COLUMNS (settings_snapshot STRING COMMENT 'Resolved settings in force, as JSON')
    """.strip()


def build_run_log_insert(
    candidate_count: int,
    outcome: str,
    note: str = "",
    app_catalog: str | None = None,
    app_schema: str | None = None,
    settings_snapshot: str = "",
) -> str:
    table = qualified_table(TABLE_SCAN_RUN_LOG, app_catalog, app_schema)
    escaped_note = note.replace("'", "''")
    escaped_settings = settings_snapshot.replace("'", "''")
    return f"""
    INSERT INTO {table} (run_at, candidate_count, outcome, note, settings_snapshot)
    VALUES (
      current_timestamp(), {candidate_count}, '{outcome}', '{escaped_note}',
      '{escaped_settings}'
    )
    """.strip()
