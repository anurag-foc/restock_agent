"""`sim_run` and `sim_run_pair` — the only things a simulation writes.

Everything else about a run happens in memory (see `run.py`). These two tables exist because a
result nobody can go back to is not a measurable performance indicator, it is a number someone
remembers.

**`sim_run` stores the full settings snapshot, not just the engine.** A net value computed under
a 14% holding rate is a different number from one computed under 20%, and "the defaults at the
time" is precisely what will have moved by the time anyone asks why a chart looked the way it
did. Same argument as `jobs/run_log.py`'s `settings_snapshot`, and the same reason.

**`sim_run_pair` is what makes a bad score diagnosable rather than merely reportable.** A run
that reports ₹4 cr of missed value and cannot say which pairs they were teaches nobody anything.
"""

from __future__ import annotations

import hashlib
import json

from agentic_restock.config import qualified_table

TABLE_SIM_RUN = "sim_run"
TABLE_SIM_RUN_PAIR = "sim_run_pair"


def run_id(*, engine: str, budget: int, settings_json: str, label: str = "") -> str:
    """Deterministic from the inputs, so an identical re-run is idempotent.

    Two runs of the same engine on the same world under the same policy must produce the same
    id: the generated world is deterministic, so a second row would be a duplicate of the
    first, not a second observation.
    """
    payload = json.dumps(
        {"engine": engine, "budget": budget, "settings": settings_json, "label": label},
        sort_keys=True,
    )
    return "SIM-" + hashlib.sha256(payload.encode()).hexdigest()[:12].upper()


def build_sim_run_table_ddl(catalog: str | None = None, schema: str | None = None) -> str:
    table = qualified_table(TABLE_SIM_RUN, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      RUN_ID STRING COMMENT 'Deterministic from engine + budget + settings',
      BATCH_ID STRING COMMENT 'Groups the engines compared against one world',
      RUN_TS TIMESTAMP,
      LABEL STRING COMMENT 'What the operator called this comparison',
      CONSUMPTION_MODEL STRING COMMENT 'The engine under test',
      BUDGET INT COMMENT 'Output budget in force -- findings a PM sees per run',
      SETTINGS_JSON STRING COMMENT 'Resolved policy for this run; the number is only comparable within it',
      PAIRS_TOTAL INT,
      PAIRS_AT_RISK INT COMMENT 'Pairs that would really run short -- the denominator for recall',
      CAUGHT INT,
      MISSED INT,
      FALSE_ALARMS INT,
      CORRECTLY_QUIET INT,
      DETECTOR_CAUGHT INT COMMENT 'Real shortages a scanner spotted, before the output budget',
      DETECTOR_FALSE_ALARMS INT COMMENT 'The same for false alarms',
      FINDINGS_DETECTED INT,
      FINDINGS_SELECTED INT,
      VALUE_DELIVERED DOUBLE COMMENT 'Sum of decision_value on pairs the PM saw and that were real',
      VALUE_MISSED DOUBLE COMMENT 'Exposure of pairs that needed action and got none',
      VALUE_WASTED DOUBLE COMMENT 'Action cost spent on pairs that were fine',
      NET_VALUE DOUBLE COMMENT 'SIMULATED, not measured -- see docs/simulation_feature_design.md §2.2',
      DETECTOR_NET_VALUE DOUBLE COMMENT 'The same if every finding reached the PM',
      BUDGET_COST DOUBLE COMMENT 'What the output budget costs: detector net less net',
      RECALL DOUBLE,
      PRECISION DOUBLE,
      DETECTOR_RECALL DOUBLE,
      DETECTOR_PRECISION DOUBLE,
      UNSCORED_FINDINGS INT COMMENT 'Supplier-grain findings pair-keyed truth cannot judge'
    )
    COMMENT 'One row per simulated engine run. Figures are simulated, never measured savings.'
    """.strip()


def build_sim_run_pair_table_ddl(catalog: str | None = None, schema: str | None = None) -> str:
    table = qualified_table(TABLE_SIM_RUN_PAIR, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      RUN_ID STRING,
      PART_ID STRING,
      WAREHOUSE_ID STRING,
      REGIME STRING COMMENT 'The demand pattern this pair was drawn from',
      WILL_RUN_SHORT BOOLEAN COMMENT 'Truth: would really run out before a replenishment lands',
      SHORTFALL_QTY DOUBLE,
      DETECTED BOOLEAN COMMENT 'A scanner found it',
      SELECTED BOOLEAN COMMENT 'It survived the output budget and reached the PM',
      CELL STRING COMMENT 'CAUGHT / MISSED / FALSE_ALARM / CORRECTLY_QUIET, scored on SELECTED',
      VALUE DOUBLE,
      DETECT_CELL STRING COMMENT 'The same scored on DETECTED, budget ignored',
      DETECT_VALUE DOUBLE,
      EXPOSURE DOUBLE,
      ACTION_COST DOUBLE,
      PLANTED STRING COMMENT 'Scenario ids planted here, comma separated. Context, not the label',
      FINDING_TYPES STRING
    )
    COMMENT 'One row per (run, part, warehouse). Makes a bad score diagnosable.'
    """.strip()


def build_sim_run_migration(catalog: str | None = None, schema: str | None = None) -> str:
    """Widen a `sim_run` created before the detector counts existed.

    `CREATE TABLE IF NOT EXISTS` will not add a column to a table that is already there, so a
    results table from an earlier deploy needs one ALTER. Safe to run every time -- the caller
    tolerates the "column already exists" failure rather than checking first, the same way
    `jobs/run_log.py` does.
    """
    table = qualified_table(TABLE_SIM_RUN, catalog, schema)
    return f"""
    ALTER TABLE {table} ADD COLUMNS (
      DETECTOR_CAUGHT INT COMMENT 'Real shortages a scanner spotted, before the output budget',
      DETECTOR_FALSE_ALARMS INT COMMENT 'The same for false alarms'
    )
    """.strip()


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_sim_run_insert(
    row: dict,
    *,
    run_identifier: str,
    batch_id: str,
    label: str,
    settings_json: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    table = qualified_table(TABLE_SIM_RUN, catalog, schema)
    columns = [
        "PAIRS_TOTAL", "PAIRS_AT_RISK", "CAUGHT", "MISSED", "FALSE_ALARMS",
        "CORRECTLY_QUIET", "DETECTOR_CAUGHT", "DETECTOR_FALSE_ALARMS",
        "FINDINGS_DETECTED", "FINDINGS_SELECTED",
        "VALUE_DELIVERED", "VALUE_MISSED", "VALUE_WASTED", "NET_VALUE",
        "DETECTOR_NET_VALUE", "BUDGET_COST", "RECALL", "PRECISION",
        "DETECTOR_RECALL", "DETECTOR_PRECISION", "UNSCORED_FINDINGS",
    ]
    values = ", ".join(str(row[c]) for c in columns)
    return f"""
    INSERT INTO {table} (
      RUN_ID, BATCH_ID, RUN_TS, LABEL, CONSUMPTION_MODEL, BUDGET, SETTINGS_JSON,
      {', '.join(columns)}
    )
    VALUES (
      {_sql_str(run_identifier)}, {_sql_str(batch_id)}, current_timestamp(),
      {_sql_str(label)}, {_sql_str(row["CONSUMPTION_MODEL"])}, {int(row["BUDGET"])},
      {_sql_str(settings_json)},
      {values}
    )
    """.strip()


def build_sim_run_pair_insert(
    run_identifier: str,
    pairs: list,
    *,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    """One multi-row INSERT. 278 single-row inserts against a warehouse is minutes, not seconds."""
    table = qualified_table(TABLE_SIM_RUN_PAIR, catalog, schema)
    rows = []
    for p in pairs:
        rows.append(
            "("
            + ", ".join(
                [
                    _sql_str(run_identifier),
                    _sql_str(p.part_id),
                    _sql_str(p.warehouse_id),
                    _sql_str(p.regime),
                    "true" if p.will_run_short else "false",
                    f"{p.shortfall_qty:.4f}",
                    "true" if p.detected else "false",
                    "true" if p.selected else "false",
                    _sql_str(p.cell),
                    f"{p.value:.4f}",
                    _sql_str(p.detect_cell),
                    f"{p.detect_value:.4f}",
                    f"{p.exposure:.4f}",
                    f"{p.action_cost:.4f}",
                    _sql_str(",".join(p.planted)),
                    _sql_str(",".join(p.finding_types)),
                ]
            )
            + ")"
        )
    return f"""
    INSERT INTO {table} (
      RUN_ID, PART_ID, WAREHOUSE_ID, REGIME, WILL_RUN_SHORT, SHORTFALL_QTY,
      DETECTED, SELECTED, CELL, VALUE, DETECT_CELL, DETECT_VALUE,
      EXPOSURE, ACTION_COST, PLANTED, FINDING_TYPES
    )
    VALUES {', '.join(rows)}
    """.strip()


def build_delete_run(
    run_identifier: str, *, catalog: str | None = None, schema: str | None = None
) -> list[str]:
    """Clear a run before rewriting it.

    `run_id` is deterministic, so re-running the same configuration is a *replacement*, not a
    second observation — the world is identical and a second row would double-count it in any
    average taken over the table.
    """
    run = qualified_table(TABLE_SIM_RUN, catalog, schema)
    pair = qualified_table(TABLE_SIM_RUN_PAIR, catalog, schema)
    return [
        f"DELETE FROM {run} WHERE RUN_ID = {_sql_str(run_identifier)}",
        f"DELETE FROM {pair} WHERE RUN_ID = {_sql_str(run_identifier)}",
    ]
