"""Build `part_position` and `supplier_performance` — the corrected-measures layer.

`part_position` is the signal board's successor, and the difference is what it does *not* do. The
board mixed measurement with judgement: it computed burn, lead time, transfer options, cascade
exposure and a ranking hint in one wide row per (part, warehouse), which is how six of the eight
nuances ended up as columns that could never be the reason something was flagged
(docs/intelligence_layer_design.md §1).

This table holds **only corrected measurement**: what is on hand, how fast it is really moving,
how long the supplier really takes, and how well each of those is known. No exposure, no
ranking, no fix. Those are the scanners' job, and keeping them out is what lets a scanner run at
its own natural grain instead of being flattened into this one.

Shape follows `signal_board.py`'s convention -- SQL as pure string builders, estimator work as
pure functions over DataFrames -- so everything here is testable without a cluster. The notebook
in `notebooks/positions/` is the only Spark-aware part.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from agentic_restock import settings as st
from agentic_restock.config import (
    TABLE_BOM,
    TABLE_DIM_PART,
    TABLE_DIM_PLANT,
    TABLE_DIM_SUPPLIER,
    TABLE_DIM_WAREHOUSE,
    TABLE_FACT_INVENTORY_SNAPSHOT,
    TABLE_FACT_INVENTORY_TRANSACTION,
    TABLE_FACT_PROCUREMENT,
    TABLE_SUPPLIER_CONTRACT,
    qualified_dim_table,
    qualified_fact_table,
    qualified_table,
)
from agentic_restock.estimators import burn as e1
from agentic_restock.estimators import cascade as cascade_model
from agentic_restock.estimators import leadtime as e2
from agentic_restock.estimators import risk
from agentic_restock.generation import policy

TABLE_FACT_SUPPLIER_DELIVERY = "fact_supplier_delivery"
TABLE_FACT_SUPPLIER_QUALITY = "fact_supplier_quality"

PART_POSITION_TABLE = "part_position"
SUPPLIER_PERFORMANCE_TABLE = "supplier_performance"

# How far back the burn estimator reads. Three years is what the seasonal index needs; reading
# less silently disables the seasonal branch.
ISSUE_HISTORY_DAYS = 1100

# The frozen window a shortage is assessed against. Not 60 days: a plant at line rate consumes
# more in two months than any component buffer could cover, so a long horizon makes every cascade
# finding enormous and swamps every other signal type in the ranking. A fortnight is the usual
# MRP frozen window and keeps the arithmetic legible.
CASCADE_HORIZON_DAYS = 14

PARENT_CASCADE_TABLE = "parent_cascade"

# Only these warehouses build assemblies, so only these can have a production cascade.
PRODUCTION_WAREHOUSE_TYPE = "PLANT_STORE"


# ---------------------------------------------------------------------------
# Read queries -- pure string builders, no Spark
# ---------------------------------------------------------------------------


def build_issue_history_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
) -> str:
    """Daily ISSUE quantity per (part, warehouse, date).

    Returns one row per day a part actually moved, not a dense grid: the estimator densifies
    per pair, and shipping ~200K rows beats shipping the ~300K a dense grid would need.
    """
    txn = qualified_fact_table(TABLE_FACT_INVENTORY_TRANSACTION, gold_catalog, facts_schema)
    part = qualified_dim_table(TABLE_DIM_PART, gold_catalog, dim_schema)
    warehouse = qualified_dim_table(TABLE_DIM_WAREHOUSE, gold_catalog, dim_schema)

    return f"""
    SELECT
        p.PART_ID,
        w.WAREHOUSE_ID,
        t.TRANSACTION_DATE_KEY,
        SUM(t.QUANTITY) AS ISSUE_QTY
    FROM {txn} t
    JOIN {part} p ON p.PART_KEY = t.PART_KEY AND p.IS_CURRENT = true
    JOIN {warehouse} w ON w.WAREHOUSE_KEY = t.WAREHOUSE_KEY
    WHERE t.TRANSACTION_TYPE = 'ISSUE'
      AND t.TRANSACTION_DATE_KEY > 0
    GROUP BY 1, 2, 3
    """.strip()


def build_current_position_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
    *,
    has_warehouse_key: bool = True,
) -> str:
    """Latest snapshot per (part, warehouse), plus inbound quantity from open POs.

    `fact_inventory_snapshot` is a *daily* fact, so the latest row per pair is taken with
    ROW_NUMBER. Tiebreak on the surrogate key as well as the date: two rows can share a
    SNAPSHOT_DATE_KEY (a same-day correction inserted after the original) and ordering by date
    alone picks between them nondeterministically.

    Inbound comes from `fact_procurement`'s PENDING_QTY. That table cannot serve lead-time
    history -- it has no actual-receipt date -- but the open pipeline is exactly what it is good
    for, and availability is on-hand PLUS on-order.

    **Inbound is attributed by WAREHOUSE_KEY where the column exists, and falls back to the
    plant link where it does not** (`schema_changes_gold_dev_analytics.md` §1.3, added to the
    replica 2026-09-05; still absent in `gold_dev`). The fallback ties an open PO to a receiving
    warehouse through `dim_warehouse.LINKED_PLANT_ID`, which regional DCs do not have, so their
    inbound reads as zero and their availability is understated. Summing a part's inbound across
    all its warehouses instead would be worse: it would count the same order several times and
    make every location look covered.

    Why it matters, measured on this dataset: without warehouse attribution the false-positive
    rate was 27% against 5% with it, and the run produced 183 findings rather than 76. Crying
    wolf a quarter of the time is the worst available failure for a product whose whole claim is
    that it does not spam you.
    """
    # `gold_dev` has no fact_procurement.WAREHOUSE_KEY, and referencing a missing column is a
    # parse error rather than a NULL -- so the column is spliced in only when it exists, and the
    # plant-link fallback carries the other catalog unchanged.
    warehouse_key_expr = "pr.WAREHOUSE_KEY" if has_warehouse_key else "CAST(NULL AS BIGINT)"

    snapshot = qualified_fact_table(TABLE_FACT_INVENTORY_SNAPSHOT, gold_catalog, facts_schema)
    procurement = qualified_fact_table(TABLE_FACT_PROCUREMENT, gold_catalog, facts_schema)
    part = qualified_dim_table(TABLE_DIM_PART, gold_catalog, dim_schema)
    warehouse = qualified_dim_table(TABLE_DIM_WAREHOUSE, gold_catalog, dim_schema)
    plant = qualified_dim_table(TABLE_DIM_PLANT, gold_catalog, dim_schema)

    return f"""
    WITH latest AS (
        SELECT s.*,
            ROW_NUMBER() OVER (
                PARTITION BY s.PART_KEY, s.WAREHOUSE_KEY
                ORDER BY s.SNAPSHOT_DATE_KEY DESC, s.INVENTORY_SNAPSHOT_KEY DESC
            ) AS rn
        FROM {snapshot} s
    ),
    inbound AS (
        SELECT
            pr.PART_KEY,
            -- Prefer the explicit destination; fall back to the plant link for any row (or any
            -- catalog) that does not carry one. COALESCE rather than a branch so the same SQL
            -- runs against gold_dev, where the column does not exist at all -- see the
            -- `has_warehouse_key` guard the caller applies.
            COALESCE({warehouse_key_expr}, w2.WAREHOUSE_KEY) AS WAREHOUSE_KEY,
            SUM(pr.PENDING_QTY) AS in_transit_qty
        FROM {procurement} pr
        LEFT JOIN {plant} pl ON pl.PLANT_KEY = pr.PLANT_KEY
        LEFT JOIN {warehouse} w2 ON w2.LINKED_PLANT_ID = pl.PLANT_ID
        WHERE pr.STATUS IN ('ISSUED', 'PARTIAL') AND pr.PENDING_QTY > 0
        GROUP BY 1, 2
    )
    SELECT
        p.PART_ID, w.WAREHOUSE_ID,
        l.PART_KEY, l.WAREHOUSE_KEY,
        l.QUANTITY_ON_HAND, l.SAFETY_STOCK_QTY, l.MAX_STOCK_LEVEL,
        l.AVG_DAILY_CONSUMPTION AS NAIVE_DAILY_CONSUMPTION,
        COALESCE(i.in_transit_qty, 0) AS IN_TRANSIT_QTY,
        p.UNIT_COST, p.CRITICALITY_CLASS, p.BOM_LEVEL, p.ABC_CLASS,
        w.WAREHOUSE_TYPE, w.OPERATIONAL_STATUS
    FROM latest l
    JOIN {part} p ON p.PART_KEY = l.PART_KEY AND p.IS_CURRENT = true
    JOIN {warehouse} w ON w.WAREHOUSE_KEY = l.WAREHOUSE_KEY
    LEFT JOIN inbound i
        ON i.PART_KEY = l.PART_KEY
       AND i.WAREHOUSE_KEY = l.WAREHOUSE_KEY
    WHERE l.rn = 1
      AND p.LIFECYCLE_STATUS = 'ACTIVE'
      AND w.OPERATIONAL_STATUS = 'ACTIVE'
    """.strip()


def build_delivery_history_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
) -> str:
    """Captured deliveries with planned-vs-actual dates — E2's only usable input.

    `PART_KEY` is the column this project added to the replica; without it lead time collapses to
    one blended figure per supplier, which describes no individual part
    (docs/schema_changes_gold_dev_analytics.md §1.2).
    """
    delivery = qualified_fact_table(TABLE_FACT_SUPPLIER_DELIVERY, gold_catalog, facts_schema)
    part = qualified_dim_table(TABLE_DIM_PART, gold_catalog, dim_schema)
    supplier = qualified_dim_table(TABLE_DIM_SUPPLIER, gold_catalog, dim_schema)

    return f"""
    SELECT
        p.PART_ID,
        s.SUPPLIER_ID,
        s.SUPPLIER_TYPE AS SUPPLIER_CATEGORY,
        d.DELIVERY_DATE_KEY,
        d.PLANNED_DATE_KEY,
        d.DELAY_DAYS,
        d.QUANTITY,
        d.DAMAGED_QTY,
        d.SHORT_QTY,
        d.FREIGHT_COST,
        d.OTD_FLAG
    FROM {delivery} d
    JOIN {part} p ON p.PART_KEY = d.PART_KEY AND p.IS_CURRENT = true
    JOIN {supplier} s ON s.SUPPLIER_KEY = d.SUPPLIER_KEY AND s.IS_CURRENT = true
    WHERE d.DELIVERY_DATE_KEY > 0 AND d.PLANNED_DATE_KEY > 0
    """.strip()


def build_production_plan_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
    horizon_days: int = CASCADE_HORIZON_DAYS,
) -> str:
    """Planned vehicles per model over the forward window, from the production plan.

    Reads rows dated FORWARD of today. `fact_production_execution` extends past the present --
    history carries planned and actual, the forward window carries planned only -- and that is
    what makes a cascade about "what are we about to fail to build" rather than only "what did
    we fail to build", which is the 72%-find-out-too-late gap in market_evidence_phase1.md §1.
    """
    execution = f"{gold_catalog or 'gold_dev'}.manufacturing_analytics.fact_production_execution"
    model = qualified_dim_table("dim_vehicle_model", gold_catalog, dim_schema)

    return f"""
    SELECT
        m.VEHICLE_MODEL_ID AS MODEL_ID,
        SUM(e.PLANNED_QTY) AS PLANNED_UNITS
    FROM {execution} e
    JOIN {model} m ON m.MODEL_KEY = e.MODEL_KEY AND m.IS_CURRENT = true
    WHERE e.EXECUTION_DATE_KEY > CAST(date_format(current_date(), 'yyyyMMdd') AS INT)
      AND e.EXECUTION_DATE_KEY <= CAST(
            date_format(date_add(current_date(), {horizon_days}), 'yyyyMMdd') AS INT)
    GROUP BY 1
    """.strip()


def build_bom_query(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    """Part-to-part BOM edges. Multi-level: a sub-assembly is both a parent and a child."""
    bom = qualified_table(TABLE_BOM, app_catalog, app_schema)
    return f"""
    SELECT fg_part_id AS PARENT_PART_ID,
           component_part_id AS CHILD_PART_ID,
           qty_per_unit AS QTY_PER_UNIT
    FROM {bom}
    """.strip()


def build_model_bom_query(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    """Model -> top-level assembly bridge. Without it a plan in vehicle models cannot become a
    part requirement (docs/schema_changes_gold_dev_analytics.md §2.1)."""
    model_bom = qualified_table("dim_model_bom", app_catalog, app_schema)
    return f"""
    SELECT MODEL_ID, FG_PART_ID, QTY_PER_VEHICLE
    FROM {model_bom}
    WHERE IS_CURRENT = true
    """.strip()


def build_contract_query(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    contract = qualified_table(TABLE_SUPPLIER_CONTRACT, app_catalog, app_schema)
    return f"""
    SELECT part_id AS PART_ID, supplier_id AS SUPPLIER_ID,
           lead_time_days AS CONTRACTED_LEAD_DAYS,
           moq AS MOQ, pack_size AS PACK_SIZE,
           unit_cost AS CONTRACT_UNIT_COST, is_preferred AS IS_PREFERRED
    FROM {contract}
    """.strip()


# ---------------------------------------------------------------------------
# Estimation -- pure functions over DataFrames
# ---------------------------------------------------------------------------


def _date_key_to_ordinal(date_key: int) -> int:
    year, month, day = date_key // 10000, (date_key // 100) % 100, date_key % 100
    return date(year, month, day).toordinal()


def densify_issues(
    issue_rows: pd.DataFrame, *, as_of: date, history_days: int = ISSUE_HISTORY_DAYS
) -> dict[tuple[str, str], np.ndarray]:
    """Sparse (part, warehouse, date, qty) rows -> a dense daily array per pair.

    Densifying matters: the zero days ARE the signal for intermittency, and a sparse series
    silently looks like a dense one with a higher mean. Reading only the days a part moved would
    make every part look smooth.
    """
    end = as_of.toordinal()
    start = end - history_days + 1

    series: dict[tuple[str, str], np.ndarray] = {}
    if issue_rows.empty:
        return series

    frame = issue_rows.copy()
    frame["ordinal"] = frame["TRANSACTION_DATE_KEY"].astype(int).map(_date_key_to_ordinal)
    frame = frame[(frame["ordinal"] >= start) & (frame["ordinal"] <= end)]

    for (part_id, warehouse_id), group in frame.groupby(["PART_ID", "WAREHOUSE_ID"]):
        dense = np.zeros(history_days, dtype=float)
        idx = group["ordinal"].to_numpy() - start
        np.add.at(dense, idx, group["ISSUE_QTY"].to_numpy(dtype=float))
        series[(part_id, warehouse_id)] = dense

    return series


def delivery_observations(
    delivery_rows: pd.DataFrame, *, as_of: date
) -> list[e2.DeliveryObservation]:
    """Delivery rows -> E2 observations, with age measured from `as_of`."""
    if delivery_rows.empty:
        return []

    end = as_of.toordinal()
    observations: list[e2.DeliveryObservation] = []
    for row in delivery_rows.itertuples(index=False):
        arrival = _date_key_to_ordinal(int(row.DELIVERY_DATE_KEY))
        observations.append(
            e2.DeliveryObservation(
                part_id=row.PART_ID,
                supplier_id=row.SUPPLIER_ID,
                supplier_category=row.SUPPLIER_CATEGORY,
                delay_days=float(row.DELAY_DAYS),
                days_ago=float(max(0, end - arrival)),
            )
        )
    return observations


def build_supplier_performance(
    delivery_rows: pd.DataFrame,
    contract_rows: pd.DataFrame,
    *,
    as_of: date,
) -> pd.DataFrame:
    """One row per contracted (part, supplier): E2's estimate plus observed quality.

    Takes no settings, unlike `build_part_position`. E2's two knobs (`leadtime_model`,
    `leadtime_recency`) were removed with the rest of the forecasting options, and lead time
    has no engine choice to replace them -- it estimates a distribution over observed
    deliveries rather than forecasting anything, so there is nothing to select between.
    """
    observations = delivery_observations(delivery_rows, as_of=as_of)
    categories = (
        delivery_rows.drop_duplicates("SUPPLIER_ID")
        .set_index("SUPPLIER_ID")["SUPPLIER_CATEGORY"]
        .to_dict()
        if not delivery_rows.empty
        else {}
    )

    quality: dict[tuple[str, str], dict] = {}
    if not delivery_rows.empty:
        grouped = delivery_rows.groupby(["PART_ID", "SUPPLIER_ID"])
        for key, group in grouped:
            inspected = float(group["QUANTITY"].sum())
            defects = float(group["DAMAGED_QTY"].fillna(0).sum() + group["SHORT_QTY"].fillna(0).sum())
            quality[key] = {
                "reject_rate": defects / inspected if inspected > 0 else 0.0,
                "mean_freight_cost": float(group["FREIGHT_COST"].fillna(0).mean()),
            }

    rows: list[dict] = []
    for contract in contract_rows.itertuples(index=False):
        part_id, supplier_id = contract.PART_ID, contract.SUPPLIER_ID
        estimate = e2.estimate_lead_time(
            observations,
            part_id=part_id,
            supplier_id=supplier_id,
            supplier_category=categories.get(supplier_id, "UNKNOWN"),
            contracted_days=float(contract.CONTRACTED_LEAD_DAYS),
        )
        observed = quality.get((part_id, supplier_id), {})
        rows.append(
            {
                "PART_ID": part_id,
                "SUPPLIER_ID": supplier_id,
                "CONTRACTED_LEAD_DAYS": float(contract.CONTRACTED_LEAD_DAYS),
                "MU_LEAD_DAYS": estimate.mu_lead,
                "SIGMA_LEAD_DAYS": estimate.sigma_lead,
                "P90_LEAD_DAYS": estimate.p90_lead,
                "DRIFT_DAYS": estimate.drift_days,
                "OTD_RATE": estimate.otd_rate,
                "LEAD_TIER": estimate.tier,
                "LEAD_OBSERVATIONS": estimate.observations,
                "IS_MEASURED": estimate.is_measured,
                "REJECT_RATE": observed.get("reject_rate", 0.0),
                "MEAN_FREIGHT_COST": observed.get("mean_freight_cost", 0.0),
                "MOQ": int(contract.MOQ),
                "PACK_SIZE": int(contract.PACK_SIZE),
                "CONTRACT_UNIT_COST": float(contract.CONTRACT_UNIT_COST),
                "IS_PREFERRED": bool(contract.IS_PREFERRED),
            }
        )

    return pd.DataFrame(rows)


def build_part_position(
    position_rows: pd.DataFrame,
    issue_rows: pd.DataFrame,
    supplier_performance: pd.DataFrame,
    *,
    as_of: date,
    history_days: int = ISSUE_HISTORY_DAYS,
    settings: st.Settings | None = None,
) -> pd.DataFrame:
    """One row per (part, warehouse): corrected measures and how well each is known."""
    cfg = settings or st.DEFAULTS
    series = densify_issues(issue_rows, as_of=as_of, history_days=history_days)

    preferred = (
        supplier_performance[supplier_performance["IS_PREFERRED"]]
        .set_index("PART_ID")
        .to_dict("index")
        if not supplier_performance.empty
        else {}
    )

    rows: list[dict] = []
    for position in position_rows.itertuples(index=False):
        part_id, warehouse_id = position.PART_ID, position.WAREHOUSE_ID
        supplier = preferred.get(part_id, {})

        mu_lead = float(supplier.get("MU_LEAD_DAYS", 0.0) or 0.0)
        sigma_lead = float(supplier.get("SIGMA_LEAD_DAYS", 0.0) or 0.0)
        horizon = round(mu_lead) if mu_lead > 0 else 30

        issues = series.get((part_id, warehouse_id))
        estimate = e1.estimate_burn(
            issues if issues is not None else np.zeros(1),
            end_date=as_of,
            horizon_days=horizon,
            model=cfg.consumption_model,
        )

        on_hand = int(position.QUANTITY_ON_HAND or 0)
        in_transit = int(position.IN_TRANSIT_QTY or 0)
        available = on_hand + in_transit

        # NULL rather than infinity when nothing is moving. A sentinel large number would be
        # silently ranked and compared; a NULL forces the dead-capital case to be handled as the
        # different question it is.
        days_of_cover = available / estimate.forward_burn if estimate.forward_burn > 0 else None

        target_cover = policy.target_cover_days(
            mu_lead or 30.0, sigma_lead, position.CRITICALITY_CLASS
        )

        rows.append(
            {
                "PART_ID": part_id,
                "WAREHOUSE_ID": warehouse_id,
                "PART_KEY": int(position.PART_KEY),
                "WAREHOUSE_KEY": int(position.WAREHOUSE_KEY),
                # --- position ------------------------------------------------
                "ON_HAND_QTY": on_hand,
                "IN_TRANSIT_QTY": in_transit,
                "AVAILABLE_QTY": available,
                "SAFETY_STOCK_QTY": int(position.SAFETY_STOCK_QTY or 0),
                "MAX_STOCK_LEVEL": int(position.MAX_STOCK_LEVEL or 0),
                "UNIT_COST": float(position.UNIT_COST or 0.0),
                "CRITICALITY_CLASS": position.CRITICALITY_CLASS,
                "BOM_LEVEL": int(position.BOM_LEVEL or 0),
                # Carried through because the cascade only applies where assemblies are BUILT.
                "WAREHOUSE_TYPE": position.WAREHOUSE_TYPE,
                # --- E1 ------------------------------------------------------
                "FORWARD_BURN": estimate.forward_burn,
                "BURN_LEVEL": estimate.level,
                "SIGMA_D": estimate.sigma_d,
                "BURN_CONFIDENCE": estimate.confidence,
                "BURN_METHOD": estimate.method,
                "ZERO_DAY_FRACTION": estimate.zero_day_fraction,
                "SEASONAL_INDEX_AHEAD": estimate.seasonal_index_ahead,
                # The snapshot's own flat average, kept for comparison. It is the number the old
                # board used, and carrying both makes the correction auditable rather than
                # asserted.
                "NAIVE_DAILY_CONSUMPTION": float(position.NAIVE_DAILY_CONSUMPTION or 0.0),
                # --- E2 (via the preferred supplier) -------------------------
                "PREFERRED_SUPPLIER_ID": supplier.get("SUPPLIER_ID"),
                "CONTRACTED_LEAD_DAYS": float(supplier.get("CONTRACTED_LEAD_DAYS", 0.0) or 0.0),
                "MU_LEAD_DAYS": mu_lead,
                "SIGMA_LEAD_DAYS": sigma_lead,
                "P90_LEAD_DAYS": float(supplier.get("P90_LEAD_DAYS", 0.0) or 0.0),
                "LEAD_TIER": supplier.get("LEAD_TIER"),
                "LEAD_OBSERVATIONS": int(supplier.get("LEAD_OBSERVATIONS", 0) or 0),
                # --- derived -------------------------------------------------
                "DAYS_OF_COVER": days_of_cover,
                "TARGET_COVER_DAYS": target_cover,
                "REPLENISHMENT_GAP_QTY": max(
                    round(target_cover * estimate.forward_burn) - available, 0
                ),
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Cascade and exposure -- pure functions over DataFrames
# ---------------------------------------------------------------------------


def build_parent_cascades(
    part_position: pd.DataFrame,
    plan_rows: pd.DataFrame,
    model_bom_rows: pd.DataFrame,
    bom_rows: pd.DataFrame,
) -> pd.DataFrame:
    """One row per threatened (assembly, warehouse). `value_at_risk` is counted once, here.

    Requirements come from the plan exploded model -> assembly; the constraint comes from what
    each descendant can build. Accumulating the other way -- upward from each child -- is what
    makes three components blocking one engine each claim the engine's full value.
    """
    edges = [
        cascade_model.BomEdge(
            parent_part_id=row.PARENT_PART_ID,
            child_part_id=row.CHILD_PART_ID,
            qty_per_unit=float(row.QTY_PER_UNIT),
        )
        for row in bom_rows.itertuples(index=False)
    ]
    multipliers = cascade_model.multiplier_index(edges)

    planned = dict(zip(plan_rows["MODEL_ID"], plan_rows["PLANNED_UNITS"], strict=False))
    required: dict[str, float] = {}
    for row in model_bom_rows.itertuples(index=False):
        units = float(planned.get(row.MODEL_ID, 0.0)) * float(row.QTY_PER_VEHICLE)
        required[row.FG_PART_ID] = required.get(row.FG_PART_ID, 0.0) + units

    availability: dict[str, dict[str, int]] = {}
    on_hand: dict[tuple[str, str], int] = {}
    unit_cost: dict[str, float] = {}
    production_sites: set[str] = set()
    for row in part_position.itertuples(index=False):
        availability.setdefault(row.WAREHOUSE_ID, {})[row.PART_ID] = int(row.AVAILABLE_QTY)
        on_hand[(row.PART_ID, row.WAREHOUSE_ID)] = int(row.ON_HAND_QTY)
        unit_cost[row.PART_ID] = float(row.UNIT_COST)
        if getattr(row, "WAREHOUSE_TYPE", None) == PRODUCTION_WAREHOUSE_TYPE:
            production_sites.add(row.WAREHOUSE_ID)

    # Assemblies are BUILT at plant stores, not at regional DCs. Assessing a cascade wherever a
    # child happens to be stocked reported 51 blocked parents worth Rs 618 cr -- almost all of it
    # at distribution centres that never build anything, where the parent has no stock and only a
    # few children are held.
    rows: list[dict] = []
    for warehouse_id, warehouse_availability in sorted(availability.items()):
        if production_sites and warehouse_id not in production_sites:
            continue
        for parent_part_id, required_units in sorted(required.items()):
            result = cascade_model.build_parent_cascade(
                parent_part_id=parent_part_id,
                warehouse_id=warehouse_id,
                required_units=required_units,
                parent_on_hand=on_hand.get((parent_part_id, warehouse_id), 0),
                parent_unit_cost=unit_cost.get(parent_part_id, 0.0),
                availability=warehouse_availability,
                edges=edges,
                multipliers=multipliers,
            )
            if result is None:
                continue
            rows.append(
                {
                    "PARENT_PART_ID": result.parent_part_id,
                    "WAREHOUSE_ID": result.warehouse_id,
                    "REQUIRED_UNITS": result.required_units,
                    "PARENT_ON_HAND_QTY": result.parent_on_hand,
                    "BUILDABLE_FROM_CHILDREN": result.buildable_from_children,
                    "SUPPLY_UNITS": result.supply_units,
                    "UNITS_BLOCKED": result.units_blocked,
                    "PARENT_UNIT_COST": result.parent_unit_cost,
                    "VALUE_AT_RISK": result.value_at_risk,
                    "BINDING_CHILDREN": ",".join(result.binding_children) or None,
                    "BINDING_CHILD_COUNT": len(result.binding_children),
                }
            )

    return pd.DataFrame(rows)


def attach_exposure(
    part_position: pd.DataFrame,
    parent_cascades: pd.DataFrame,
    bom_rows: pd.DataFrame,
) -> pd.DataFrame:
    """Add P(stockout), consequence and exposure to every position row.

    Consequence prefers planned production. A part that is both below its own safety stock AND
    cascade-binding therefore gets the production consequence -- the case the superseded board
    excluded from its cascade join and priced at the cost of the parts.
    """
    edges = [
        cascade_model.BomEdge(
            parent_part_id=row.PARENT_PART_ID,
            child_part_id=row.CHILD_PART_ID,
            qty_per_unit=float(row.QTY_PER_UNIT),
        )
        for row in bom_rows.itertuples(index=False)
    ]

    cascades = [
        cascade_model.ParentCascade(
            parent_part_id=row.PARENT_PART_ID,
            warehouse_id=row.WAREHOUSE_ID,
            required_units=row.REQUIRED_UNITS,
            parent_on_hand=row.PARENT_ON_HAND_QTY,
            buildable_from_children=row.BUILDABLE_FROM_CHILDREN,
            supply_units=row.SUPPLY_UNITS,
            units_blocked=row.UNITS_BLOCKED,
            parent_unit_cost=row.PARENT_UNIT_COST,
            value_at_risk=row.VALUE_AT_RISK,
            binding_children=tuple(
                (row.BINDING_CHILDREN or "").split(",") if row.BINDING_CHILDREN else ()
            ),
        )
        for row in parent_cascades.itertuples(index=False)
    ]
    consequences = cascade_model.child_consequence(cascades)
    _ = edges  # edges are needed only to build cascades; kept for signature symmetry

    enriched: list[dict] = []
    for row in part_position.itertuples(index=False):
        cascade_hit = consequences.get((row.PART_ID, row.WAREHOUSE_ID))
        assessment = risk.assess(
            forward_burn=float(row.FORWARD_BURN),
            sigma_d=float(row.SIGMA_D),
            burn_confidence=row.BURN_CONFIDENCE,
            mu_lead=float(row.MU_LEAD_DAYS) or 30.0,
            sigma_lead=float(row.SIGMA_LEAD_DAYS),
            available_qty=int(row.AVAILABLE_QTY),
            unit_cost=float(row.UNIT_COST),
            safety_stock_qty=int(row.SAFETY_STOCK_QTY),
            criticality_class=row.CRITICALITY_CLASS,
            cascade_value_at_risk=cascade_hit["value_at_risk"] if cascade_hit else None,
        )
        enriched.append(
            {
                **row._asdict(),
                "P_STOCKOUT": assessment.p_stockout,
                "MU_DEMAND_OVER_LEAD": assessment.mu_demand_over_lead,
                "SIGMA_DEMAND_OVER_LEAD": assessment.sigma_demand_over_lead,
                "CONSEQUENCE": assessment.consequence,
                "CONSEQUENCE_BASIS": assessment.consequence_basis,
                "EXPOSURE": assessment.exposure,
                "THREATENED_PARENT_PART_ID": (
                    cascade_hit["threatened_parent_part_id"] if cascade_hit else None
                ),
                "CO_BINDING_CHILDREN": (
                    ",".join(cascade_hit["co_binding_children"]) or None
                    if cascade_hit
                    else None
                ),
                # The superseded formula, for the parallel-run comparison only.
                "SUPERSEDED_SHORTFALL_EXPOSURE": risk.shortfall_exposure(
                    safety_stock_qty=int(row.SAFETY_STOCK_QTY),
                    available_qty=int(row.AVAILABLE_QTY),
                    unit_cost=float(row.UNIT_COST),
                ),
            }
        )

    return pd.DataFrame(enriched)
