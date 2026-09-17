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
TABLE_SIM_BENCHMARK = "sim_benchmark"
TABLE_SIM_SELECTION = "sim_selection"
TABLE_SIM_TYPE_SUMMARY = "sim_type_summary"


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
      UNSCORED_FINDINGS INT COMMENT 'Supplier-grain findings pair-keyed truth cannot judge',
      NON_SHORTAGE_FINDINGS INT COMMENT 'Findings whose type does not address a shortage (S4/S6/S8) -- the score says nothing about these',
      OUT_OF_SCOPE_PAIRS INT COMMENT 'In-house pairs excluded: a purchase lead time is meaningless for a part the factory builds'
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


def build_sim_benchmark_table_ddl(
    catalog: str | None = None, schema: str | None = None
) -> str:
    """The eleven planted problems and what the run did about each.

    Separate from `sim_run` because it answers a different question at a different grain.
    `sim_run` scores one number over the whole network — will these pairs run short — which
    covers three of the eight scanners. This table asks, per planted fault, whether the system
    found it, and it is the only surface that says anything at all about the other five.

    `DETAIL` is stored verbatim rather than recomputed for display. A row that reads
    "avoided the decoy WH006 and left donor WH005 at a 6% chance of stocking out" is checkable
    by a human against the warehouse; a stored PASS with the reasoning regenerated later is not
    the same claim, and the reasoning is the entire point of the row.
    """
    table = qualified_table(TABLE_SIM_BENCHMARK, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      BATCH_ID STRING COMMENT 'The run this grading belongs to',
      RUN_TS TIMESTAMP,
      LABEL STRING,
      FINDING_ID STRING COMMENT 'F1-F11, the id in generation/scenarios.py',
      NAME STRING COMMENT 'What was planted, in words',
      PROVES STRING COMMENT 'Why this case is in the catalog at all',
      FINDING_TYPE STRING COMMENT 'The scanner it belongs to',
      IS_NEGATIVE BOOLEAN COMMENT 'A pass means the system stayed QUIET on this one',
      RESULT STRING COMMENT 'PASS / FAIL / CHECK',
      SUBJECT STRING COMMENT 'The actual part, warehouse or supplier -- what makes a row auditable',
      DETAIL STRING COMMENT 'What happened, in words, stored as graded',
      SHOWN_TO_PM BOOLEAN COMMENT 'Also survived the output budget'
    )
    COMMENT 'Per-planted-problem grading. The answer sheet was fixed before the run.'
    """.strip()


def build_sim_benchmark_insert(
    checks: list,
    *,
    batch_id: str,
    label: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    table = qualified_table(TABLE_SIM_BENCHMARK, catalog, schema)
    rows = []
    for c in checks:
        rows.append(
            "("
            + ", ".join(
                [
                    _sql_str(batch_id),
                    "current_timestamp()",
                    _sql_str(label),
                    _sql_str(c.finding_id),
                    _sql_str(c.name),
                    _sql_str(c.proves),
                    _sql_str(c.finding_type),
                    "true" if c.negative else "false",
                    _sql_str(c.result),
                    _sql_str(c.subject),
                    _sql_str(c.detail),
                    "true" if c.selected else "false",
                ]
            )
            + ")"
        )
    return f"""
    INSERT INTO {table} (
      BATCH_ID, RUN_TS, LABEL, FINDING_ID, NAME, PROVES, FINDING_TYPE,
      IS_NEGATIVE, RESULT, SUBJECT, DETAIL, SHOWN_TO_PM
    )
    VALUES {', '.join(rows)}
    """.strip()


def build_sim_selection_table_ddl(
    catalog: str | None = None, schema: str | None = None
) -> str:
    """What each arm actually put in front of a person.

    The scorecard says how many were right; this says what they *were*. It is the only surface
    that shows the shape of the output rather than a count of it, and the shape is the argument:
    four items spanning four different kinds of problem, of which one is a shortage. A
    reorder-point rule's twenty rows are twenty of the same sentence, and putting the two side by
    side says more than any ratio does.

    `EXPOSURE_BASIS` is the derivation in words, carried through verbatim from the finding. There
    is no single formula across the eight types, so the number cannot be re-derived at display
    time -- the reasoning has to travel with it or the figure is unsourced on screen.
    """
    table = qualified_table(TABLE_SIM_SELECTION, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      RUN_ID STRING,
      BATCH_ID STRING,
      CONSUMPTION_MODEL STRING COMMENT 'The arm that produced it',
      RANK INT COMMENT 'Position in the list the PM would see',
      FINDING_TYPE STRING,
      SUBJECT_ID STRING COMMENT 'The part, parent, supplier or pair this is about',
      ACTION_TYPE STRING,
      ACTION_DETAIL STRING COMMENT 'The recommendation in words',
      EXPOSURE DOUBLE COMMENT 'Money at risk. SIMULATED on a generated warehouse',
      ACTION_COST DOUBLE,
      DECISION_VALUE DOUBLE COMMENT 'Exposure less the cost of acting -- what the ranking sorts on',
      EXPOSURE_BASIS STRING COMMENT 'How the figure was arrived at, in words and numbers',
      CONFIDENCE STRING
    )
    COMMENT 'One row per finding an arm surfaced. Figures are simulated, never measured.'
    """.strip()


def build_sim_selection_insert(
    findings: list,
    *,
    run_identifier: str,
    batch_id: str,
    engine: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    table = qualified_table(TABLE_SIM_SELECTION, catalog, schema)
    rows = []
    for rank, f in enumerate(findings, start=1):
        rows.append(
            "("
            + ", ".join(
                [
                    _sql_str(run_identifier),
                    _sql_str(batch_id),
                    _sql_str(engine),
                    str(rank),
                    _sql_str(f.finding_type),
                    _sql_str(f.subject_id),
                    _sql_str(f.action_type),
                    _sql_str(f.action_detail),
                    f"{float(f.exposure):.4f}",
                    f"{float(f.action_cost):.4f}",
                    f"{float(f.decision_value):.4f}",
                    _sql_str(f.exposure_basis),
                    _sql_str(f.confidence),
                ]
            )
            + ")"
        )
    if not rows:
        return ""
    return f"""
    INSERT INTO {table} (
      RUN_ID, BATCH_ID, CONSUMPTION_MODEL, RANK, FINDING_TYPE, SUBJECT_ID,
      ACTION_TYPE, ACTION_DETAIL, EXPOSURE, ACTION_COST, DECISION_VALUE,
      EXPOSURE_BASIS, CONFIDENCE
    )
    VALUES {', '.join(rows)}
    """.strip()


def build_sim_type_summary_table_ddl(
    catalog: str | None = None, schema: str | None = None
) -> str:
    """One row per finding type per run: how many, and one worked example in plain words.

    This is the benchmark chart's data, not `sim_run`'s. `sim_run` scores a shortage
    prediction question over the whole network; this table answers a different one -- across
    everything the detectors are capable of finding, how many of each kind turned up, and can a
    person verify one of them by hand. `EXAMPLE_REASONING` is generated once, in
    `simulation/reasoning.py`, and stored rather than recomputed at display time, for the same
    reason `sim_benchmark.DETAIL` is: the reasoning is the claim, and a claim that regenerates
    itself on every page load is not the same claim twice.

    Also carries the ERP arms, at one row each (their whole output is one finding type by
    construction), so the chart's reference bar comes from the same table as the rest of it
    rather than a second query.
    """
    table = qualified_table(TABLE_SIM_TYPE_SUMMARY, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      RUN_ID STRING,
      BATCH_ID STRING,
      RUN_TS TIMESTAMP,
      LABEL STRING,
      CONSUMPTION_MODEL STRING COMMENT 'The arm this row belongs to',
      FINDING_TYPE STRING,
      COUNT INT COMMENT 'How many findings of this type this arm produced, budget ignored',
      EXAMPLE_SUBJECT STRING COMMENT 'The real part, parent, supplier or pair the example is about',
      EXAMPLE_REASONING STRING COMMENT 'Plain-English, generated once and stored -- not recomputed on display',
      EXAMPLE_EXPOSURE DOUBLE COMMENT 'Money at risk on the example. SIMULATED, never measured',
      ACCURACY_NOTE STRING COMMENT 'Plain-English hit rate against ground truth, only for the three shortage-addressing types -- empty where no ground truth exists yet'
    )
    COMMENT 'Counts and one worked example per finding type per run -- the benchmark chart''s data.'
    """.strip()


def build_sim_type_summary_insert(
    rows: list[dict],
    *,
    run_identifier: str,
    batch_id: str,
    label: str,
    engine: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    """`rows` is a list of dicts with keys finding_type, count, example_subject,
    example_reasoning, example_exposure -- see `build_type_summary_rows` in run.py."""
    table = qualified_table(TABLE_SIM_TYPE_SUMMARY, catalog, schema)
    values = []
    for r in rows:
        values.append(
            "("
            + ", ".join(
                [
                    _sql_str(run_identifier),
                    _sql_str(batch_id),
                    "current_timestamp()",
                    _sql_str(label),
                    _sql_str(engine),
                    _sql_str(r["finding_type"]),
                    str(int(r["count"])),
                    _sql_str(r["example_subject"]),
                    _sql_str(r["example_reasoning"]),
                    f"{float(r['example_exposure']):.4f}",
                    _sql_str(r.get("accuracy_note", "")),
                ]
            )
            + ")"
        )
    if not values:
        return ""
    return f"""
    INSERT INTO {table} (
      RUN_ID, BATCH_ID, RUN_TS, LABEL, CONSUMPTION_MODEL, FINDING_TYPE,
      COUNT, EXAMPLE_SUBJECT, EXAMPLE_REASONING, EXAMPLE_EXPOSURE, ACCURACY_NOTE
    )
    VALUES {', '.join(values)}
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


def build_sim_type_summary_migration(
    catalog: str | None = None, schema: str | None = None
) -> str:
    """Widen a `sim_type_summary` created before the accuracy note existed.

    Same tolerated-ALTER pattern as `build_sim_run_migration` -- `CREATE TABLE IF NOT EXISTS`
    will not add a column to a table already deployed, and the caller is expected to swallow
    the "column already exists" failure on every run after the first.
    """
    table = qualified_table(TABLE_SIM_TYPE_SUMMARY, catalog, schema)
    return f"""
    ALTER TABLE {table} ADD COLUMNS (
      ACCURACY_NOTE STRING COMMENT 'Plain-English hit rate against ground truth, only for the three shortage-addressing types'
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
    selection = qualified_table(TABLE_SIM_SELECTION, catalog, schema)
    type_summary = qualified_table(TABLE_SIM_TYPE_SUMMARY, catalog, schema)
    return [
        f"DELETE FROM {type_summary} WHERE RUN_ID = {_sql_str(run_identifier)}",
        f"DELETE FROM {selection} WHERE RUN_ID = {_sql_str(run_identifier)}",
        f"DELETE FROM {run} WHERE RUN_ID = {_sql_str(run_identifier)}",
        f"DELETE FROM {pair} WHERE RUN_ID = {_sql_str(run_identifier)}",
    ]
