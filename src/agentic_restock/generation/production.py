"""Production plan and output — `fact_production_execution` and `fact_vehicle_build`.

`fact_production_execution` is the **build-target source** that retires the
`MAX_STOCK_LEVEL - QUANTITY_ON_HAND` proxy. It already exists in the replica with the right
shape (`PLANNED_QTY`/`ACTUAL_QTY` per date/plant/line/model) but `EXECUTION_DATE_KEY` is `-1` on
every row and `PLANNED_QTY` is 0% populated, so nothing can read a build target from it today.

Rows extend **past the present**: history carries both planned and actual, the forward window
carries planned only. That is how a production plan behaves, and it is what gives the cascade
detector something to explode. A table that stopped at today would only ever support
"what did we fail to build", never "what are we about to fail to build" — which is the whole
72%-find-out-too-late gap in docs/market_evidence_phase1.md §1.

`fact_vehicle_build` is regenerated rather than left alone: renumbering the model, plant and
line keys orphans all 1110 of its existing rows, which would violate the referential-integrity
assertion in docs/dataset_generator_spec.md §5.6.
"""

from __future__ import annotations

from functools import cache

from agentic_restock.generation import bom, dates, entities
from agentic_restock.generation.demand import PLANT_VEHICLES_PER_DAY, _rng

# The frozen window a shortage is assessed against. Not 60 days: a plant at line rate consumes
# more in two months than any component buffer could cover, so a long horizon makes every
# cascade finding enormous and swamps every other signal type in the ranking. A fortnight is the
# usual MRP frozen window and keeps the arithmetic legible.
CASCADE_HORIZON_DAYS = 14

# How far the plan extends beyond today.
PLAN_FORWARD_DAYS = 60

# Plan-vs-actual: real lines miss the plan slightly more often than they beat it.
ACTUAL_ATTAINMENT_MEAN = 0.97
ACTUAL_ATTAINMENT_CV = 0.06

HISTORY_DAYS = 1100

_SHIFTS = ("A", "B")


@cache
def _model_line_assignment() -> list[tuple[str, str, str]]:
    """(model_id, plant_id, line_id) — each line runs two models, one per shift."""
    models = entities.vehicle_models()
    lines = entities.production_lines()
    assignment: list[tuple[str, str, str]] = []
    for index, model in enumerate(models):
        line = lines[index % len(lines)]
        assignment.append((model["VEHICLE_MODEL_ID"], line["PLANT_ID"], line["LINE_ID"]))
    return assignment


@cache
def daily_plan() -> dict[str, float]:
    """Planned vehicles per day, per model. Sums to the plant's line rate."""
    models = entities.vehicle_models()
    per_model = PLANT_VEHICLES_PER_DAY / len(models)
    # Vary the mix so models are not interchangeable -- a flat plan makes every cascade
    # identical and hides whether the detector is using the plan at all.
    return {
        model["VEHICLE_MODEL_ID"]: round(per_model * (0.6 + 0.1 * (index % 9)), 1)
        for index, model in enumerate(models)
    }


def execution_rows() -> list[dict]:
    """`fact_production_execution` rows: history with actuals, forward window planned-only."""
    plan = daily_plan()
    lines = {line["LINE_ID"]: line for line in entities.production_lines()}
    plants = {plant["PLANT_ID"]: plant for plant in entities.plants()}
    model_keys = {m["VEHICLE_MODEL_ID"]: m["MODEL_KEY"] for m in entities.vehicle_models()}

    total_days = HISTORY_DAYS + PLAN_FORWARD_DAYS
    rows: list[dict] = []
    key = 1

    for day in range(total_days):
        is_future = day >= HISTORY_DAYS
        date_key = dates.day_index_key(day, HISTORY_DAYS)

        for shift_index, (model_id, plant_id, line_id) in enumerate(_model_line_assignment()):
            planned = plan[model_id]
            if is_future:
                actual = 0
                downtime = 0
                rejected = 0
            else:
                rng = _rng(model_id, line_id, "attainment", day)
                attainment = max(
                    0.0, rng.normal(ACTUAL_ATTAINMENT_MEAN, ACTUAL_ATTAINMENT_CV)
                )
                actual = round(planned * attainment)
                downtime = round(max(0.0, (1.0 - attainment) * 480))
                rejected = round(actual * 0.004)

            run_time = 480 - downtime
            availability = round(100.0 * run_time / 480, 2)
            performance = round(100.0 * min(actual / planned, 1.2), 2) if planned else 0.0
            quality = round(100.0 * (1 - rejected / actual), 2) if actual else 100.0

            rows.append(
                {
                    "PRODUCTION_EXECUTION_KEY": key,
                    "EXECUTION_ID": f"PE-{key:07d}",
                    "EXECUTION_DATE_KEY": date_key,
                    "PLANT_KEY": plants[plant_id]["PLANT_KEY"],
                    "LINE_KEY": lines[line_id]["LINE_KEY"],
                    "MODEL_KEY": model_keys[model_id],
                    "SUPERVISOR_EMPLOYEE_KEY": -1,
                    "PRODUCTION_ORDER_ID": f"PO-{date_key}-{line_id}",
                    "SHIFT_CODE": _SHIFTS[shift_index % len(_SHIFTS)],
                    "PLANNED_QTY": round(planned),
                    "ACTUAL_QTY": actual,
                    "REJECTED_QTY": rejected,
                    "DOWNTIME_MIN": downtime,
                    "RUN_TIME_MIN": run_time,
                    "AVAILABILITY_PCT": availability,
                    "PERFORMANCE_PCT": performance,
                    "QUALITY_PCT": quality,
                    "OEE_PCT": round(availability * performance * quality / 10000, 2),
                    "REWORK_QTY": round(rejected * 0.5),
                    "ENERGY_CONSUMED_KWH": round(actual * 42.5, 2),
                    "DW_SOURCE": entities.DW_SOURCE,
                }
            )
            key += 1

    return rows


def vehicle_build_rows(limit: int = 1200) -> list[dict]:
    """`fact_vehicle_build` — individual builds, sampled from recent actual output.

    Regenerated because renumbering the dimension keys orphans every existing row. Not read by
    any detector; it exists so the catalog stays referentially intact.
    """
    plan = daily_plan()
    lines = {line["LINE_ID"]: line for line in entities.production_lines()}
    plants = {plant["PLANT_ID"]: plant for plant in entities.plants()}
    model_keys = {m["VEHICLE_MODEL_ID"]: m["MODEL_KEY"] for m in entities.vehicle_models()}

    rows: list[dict] = []
    key = 1
    day = HISTORY_DAYS - 1
    while day >= 0 and len(rows) < limit:
        date_key = dates.day_index_key(day, HISTORY_DAYS)
        for model_id, plant_id, line_id in _model_line_assignment():
            if len(rows) >= limit:
                break
            rng = _rng(model_id, line_id, "build", day)
            defects = int(rng.poisson(0.4))
            dispatch_lag = 2 + int(rng.integers(0, 5))
            rows.append(
                {
                    "VEHICLE_BUILD_KEY": key,
                    "VEHICLE_KEY": key,
                    "MODEL_KEY": model_keys[model_id],
                    "PLANT_KEY": plants[plant_id]["PLANT_KEY"],
                    "LINE_KEY": lines[line_id]["LINE_KEY"],
                    "DEALER_KEY": -1,
                    "PRODUCTION_DATE_KEY": date_key,
                    "DISPATCH_DATE_KEY": dates.day_index_key(
                        min(day + dispatch_lag, HISTORY_DAYS - 1), HISTORY_DAYS
                    ),
                    "VIN": f"SYN{key:010d}",
                    "DEFECT_COUNT": defects,
                    "REWORK_FLAG": defects > 1,
                    "PDI_STATUS": "PASS" if defects <= 1 else "REWORK",
                    "ROLL_OFF_TO_DISPATCH_DAYS": dispatch_lag,
                    "DW_SOURCE": entities.DW_SOURCE,
                }
            )
            key += 1
        day -= 1
        _ = plan  # plan drives quantities elsewhere; builds are sampled, not exhaustive
    return rows


# ---------------------------------------------------------------------------
# Requirement explosion -- what the cascade detector will compute
# ---------------------------------------------------------------------------


@cache
def planned_vehicles_over_horizon(horizon_days: int = CASCADE_HORIZON_DAYS) -> dict[str, float]:
    """Planned vehicles per model over the forward window."""
    return {model_id: qty * horizon_days for model_id, qty in daily_plan().items()}


@cache
def part_requirements(horizon_days: int = CASCADE_HORIZON_DAYS) -> dict[str, float]:
    """Units of each part the forward plan requires, exploded model -> assembly -> component.

    This is the reference implementation of what the cascade detector has to do, kept here so
    the generator can assert the planted findings actually bind before anything is loaded.
    """
    planned = planned_vehicles_over_horizon(horizon_days)
    requirements: dict[str, float] = {}

    for model_id, vehicles in planned.items():
        for row in bom.model_bom():
            if row["MODEL_ID"] != model_id:
                continue
            assembly = row["FG_PART_ID"]
            assembly_units = vehicles * row["QTY_PER_VEHICLE"]
            requirements[assembly] = requirements.get(assembly, 0.0) + assembly_units

            for descendant in bom.descendants_of(assembly):
                multiplier = bom.multiplier_to_ancestor(descendant, assembly) or 1
                requirements[descendant] = (
                    requirements.get(descendant, 0.0) + assembly_units * multiplier
                )

    return requirements
