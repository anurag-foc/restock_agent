"""Scan run log — records that a run happened even when nothing needed action.

Without this, "nothing needed attention" and "the job silently broke" are
indistinguishable from the outside, and there is no way to report the
alert-fatigue counterpoint this product leans on: "we were quiet on 8 of 14
runs this week" (docs/market_evidence_phase1.md §3). One row per scan run,
written whether or not a Supervisor conversation was opened.
"""

from agentic_restock.config import qualified_table

TABLE_SCAN_RUN_LOG = "scan_run_log"


def build_run_log_table_ddl(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    table = qualified_table(TABLE_SCAN_RUN_LOG, app_catalog, app_schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      run_at TIMESTAMP COMMENT 'When this scan ran',
      candidate_count INT COMMENT 'Rows returned by rank_priority_actions this run',
      outcome STRING COMMENT 'NO_ACTION or SUPERVISOR_INVOKED',
      note STRING COMMENT 'Free-text detail, e.g. why the Supervisor call was skipped'
    )
    COMMENT 'One row per scan run, including quiet ones -- see module docstring.'
    """.strip()


def build_run_log_insert(
    candidate_count: int,
    outcome: str,
    note: str = "",
    app_catalog: str | None = None,
    app_schema: str | None = None,
) -> str:
    table = qualified_table(TABLE_SCAN_RUN_LOG, app_catalog, app_schema)
    escaped_note = note.replace("'", "''")
    return f"""
    INSERT INTO {table} (run_at, candidate_count, outcome, note)
    VALUES (current_timestamp(), {candidate_count}, '{outcome}', '{escaped_note}')
    """.strip()
