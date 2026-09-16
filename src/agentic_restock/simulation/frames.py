"""Generated rows → the query-shaped DataFrames the pipeline reads.

`jobs/positions.py` reads Spark SQL results. The generator emits table rows. This is the
adapter between them, and it is what lets a whole scan run in memory with no cluster and no
catalog — generate, estimate, scan, score, all in one process.

That matters for more than convenience. A simulation must never write to
`gold_dev_analytics`: the live pipeline reads that catalog, and a run that mutated it would
make the tool's own output depend on how many simulations somebody had run that morning.
Keeping the whole thing in DataFrames removes the possibility rather than guarding against it.

The column sets here mirror the `build_*_query` functions in `jobs/positions.py`. If one of
those queries gains a column the detectors read, this has to gain it too — which
`tests/test_positions.py` catches, since it builds its fixtures from here.
"""

from __future__ import annotations

import pandas as pd

from agentic_restock.generation import dataset, dates, entities
from agentic_restock.jobs import positions


def _lookups() -> dict[str, dict]:
    parts = entities.parts()
    warehouses = entities.warehouses()
    suppliers = entities.suppliers()
    # PLANT_KEY -> WAREHOUSE_KEY, the join the position query resolves an open PO's destination
    # through. `fact_procurement` carries a plant, not a warehouse, so this is the only route
    # from an order to the stock it will land in.
    plant_id_by_key = {p["PLANT_KEY"]: p["PLANT_ID"] for p in entities.plants()}
    warehouse_key_by_plant = {
        w["LINKED_PLANT_ID"]: w["WAREHOUSE_KEY"]
        for w in warehouses
        if w.get("LINKED_PLANT_ID")
    }
    return {
        "warehouse_key_for_plant_key": {
            plant_key: warehouse_key_by_plant[plant_id]
            for plant_key, plant_id in plant_id_by_key.items()
            if plant_id in warehouse_key_by_plant
        },
        "part_id": {p["PART_KEY"]: p["PART_ID"] for p in parts},
        "warehouse_id": {w["WAREHOUSE_KEY"]: w["WAREHOUSE_ID"] for w in warehouses},
        "supplier_id": {s["SUPPLIER_KEY"]: s["SUPPLIER_ID"] for s in suppliers},
        "supplier_category": {s["SUPPLIER_ID"]: s["SUPPLIER_TYPE"] for s in suppliers},
        "part": {p["PART_ID"]: p for p in parts},
        "warehouse": {w["WAREHOUSE_ID"]: w for w in warehouses},
    }


def build(data: dict[str, list[dict]] | None = None) -> dict[str, pd.DataFrame]:
    """Every frame a scan needs, keyed by the argument name it is passed as."""
    data = data if data is not None else dataset.build()
    look = _lookups()

    return {
        "issues": _issues(data, look),
        "position": _position(data, look),
        "delivery": _delivery(data, look),
        "contracts": _contracts(data),
        "plan": _plan(data),
        "model_bom": _model_bom(data),
        "bom": _bom(data),
    }


def _issues(data, look) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PART_ID": look["part_id"][r["PART_KEY"]],
                "WAREHOUSE_ID": look["warehouse_id"][r["WAREHOUSE_KEY"]],
                "TRANSACTION_DATE_KEY": r["TRANSACTION_DATE_KEY"],
                "ISSUE_QTY": r["QUANTITY"],
            }
            for r in data["fact_inventory_transaction"]
            if r["TRANSACTION_TYPE"] == "ISSUE"
        ]
    )


# Open PO statuses the position query counts as inbound. An order that is CLOSED or CANCELLED
# is not on its way, and a row with nothing pending has already landed.
OPEN_PO_STATUSES = ("ISSUED", "PARTIAL")


def _inbound(data, look) -> dict[tuple[int, int], int]:
    """Pending quantity per (part, warehouse), exactly as the position query's `inbound` CTE.

    This mirrors `jobs/positions.py::build_current_position_query` and previously did not, which
    was not a cosmetic drift. Summing `PENDING_QTY` by PART_KEY alone handed **every** warehouse
    holding a part the whole network's open orders, and skipping the status filter counted orders
    that were not open. Available stock came out at roughly four times on-hand, days of cover
    followed it, and S1 -- whose entire test is whether cover outlasts the lead time -- went
    quiet on pairs that were genuinely short. A simulation understated the pipeline it exists to
    measure, in the pipeline's own favour on precision and against it on recall.

    The warehouse comes from the row's own `WAREHOUSE_KEY`, falling back to the PLANT_KEY link
    -- the same COALESCE, in the same order. The fallback is not dead code: it is what the live
    query does against a `fact_procurement` written before that column existed, and reproducing
    it here is the difference between measuring the pipeline and measuring a better one.
    """
    totals: dict[tuple[int, int], int] = {}
    for row in data["fact_procurement"]:
        if row.get("STATUS") not in OPEN_PO_STATUSES:
            continue
        pending = int(row.get("PENDING_QTY") or 0)
        if pending <= 0:
            continue
        # The query's COALESCE: the explicit destination first, the plant link as the fallback
        # for any row that predates the column.
        warehouse_key = row.get("WAREHOUSE_KEY") or look["warehouse_key_for_plant_key"].get(
            row["PLANT_KEY"]
        )
        if warehouse_key is None:
            continue
        key = (row["PART_KEY"], warehouse_key)
        totals[key] = totals.get(key, 0) + pending
    return totals


def _position(data, look) -> pd.DataFrame:
    """Latest snapshot per pair, as the query's `ROW_NUMBER` would pick it."""
    snapshot = pd.DataFrame(data["fact_inventory_snapshot"])
    latest = (
        snapshot.sort_values(["SNAPSHOT_DATE_KEY", "INVENTORY_SNAPSHOT_KEY"])
        .groupby(["PART_KEY", "WAREHOUSE_KEY"], as_index=False)
        .last()
    )
    inbound = _inbound(data, look)

    rows = []
    for r in latest.itertuples(index=False):
        part_id = look["part_id"][r.PART_KEY]
        warehouse_id = look["warehouse_id"][r.WAREHOUSE_KEY]
        part = look["part"][part_id]
        warehouse = look["warehouse"][warehouse_id]
        rows.append(
            {
                "PART_ID": part_id,
                "WAREHOUSE_ID": warehouse_id,
                "PART_KEY": r.PART_KEY,
                "WAREHOUSE_KEY": r.WAREHOUSE_KEY,
                "QUANTITY_ON_HAND": r.QUANTITY_ON_HAND,
                "SAFETY_STOCK_QTY": r.SAFETY_STOCK_QTY,
                "MAX_STOCK_LEVEL": r.MAX_STOCK_LEVEL,
                "NAIVE_DAILY_CONSUMPTION": r.AVG_DAILY_CONSUMPTION,
                "IN_TRANSIT_QTY": inbound.get((r.PART_KEY, r.WAREHOUSE_KEY), 0),
                "UNIT_COST": part["UNIT_COST"],
                "CRITICALITY_CLASS": part["CRITICALITY_CLASS"],
                "BOM_LEVEL": part["BOM_LEVEL"],
                "ABC_CLASS": part["ABC_CLASS"],
                # The real type, not a constant. The cascade rollup is restricted to
                # PLANT_STORE, so stamping every row REGIONAL_DC would silently switch off
                # S2 and make a whole finding type unscoreable.
                "WAREHOUSE_TYPE": warehouse["WAREHOUSE_TYPE"],
                "OPERATIONAL_STATUS": warehouse["OPERATIONAL_STATUS"],
            }
        )
    return pd.DataFrame(rows)


def _delivery(data, look) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PART_ID": look["part_id"][r["PART_KEY"]],
                "SUPPLIER_ID": look["supplier_id"][r["SUPPLIER_KEY"]],
                "SUPPLIER_CATEGORY": look["supplier_category"][
                    look["supplier_id"][r["SUPPLIER_KEY"]]
                ],
                "DELIVERY_DATE_KEY": r["DELIVERY_DATE_KEY"],
                "PLANNED_DATE_KEY": r["PLANNED_DATE_KEY"],
                "DELAY_DAYS": r["DELAY_DAYS"],
                "QUANTITY": r["QUANTITY"],
                "DAMAGED_QTY": r["DAMAGED_QTY"],
                "SHORT_QTY": r["SHORT_QTY"],
                "FREIGHT_COST": r["FREIGHT_COST"],
                "OTD_FLAG": r["OTD_FLAG"],
            }
            for r in data["fact_supplier_delivery"]
        ]
    )


def _contracts(data) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PART_ID": r["part_id"],
                "SUPPLIER_ID": r["supplier_id"],
                "CONTRACTED_LEAD_DAYS": r["lead_time_days"],
                "MOQ": r["moq"],
                "PACK_SIZE": r["pack_size"],
                "CONTRACT_UNIT_COST": r["unit_cost"],
                "IS_PREFERRED": r["is_preferred"],
            }
            for r in data["dim_supplier_contract"]
        ]
    )


def _plan(data, horizon_days: int = positions.CASCADE_HORIZON_DAYS) -> pd.DataFrame:
    """Planned vehicles per model over the FORWARD window only.

    The forward restriction is the whole point of a cascade — "what are we about to fail to
    build", not "what did we fail to build". `fact_production_execution` carries both, so a
    simulation that aggregated the lot would report three years of history as pending demand
    and every parent would look catastrophically blocked.
    """
    models = {m["MODEL_KEY"]: m["VEHICLE_MODEL_ID"] for m in entities.vehicle_models()}
    today_key = dates.date_key(dates.TODAY)
    end_key = dates.date_key(dates.TODAY + dates.timedelta(days=horizon_days))

    planned: dict[str, float] = {}
    for row in data["fact_production_execution"]:
        if not today_key < row["EXECUTION_DATE_KEY"] <= end_key:
            continue
        model_id = models.get(row["MODEL_KEY"])
        if model_id is None:
            continue
        planned[model_id] = planned.get(model_id, 0.0) + float(row["PLANNED_QTY"] or 0)

    return pd.DataFrame(
        [{"MODEL_ID": k, "PLANNED_UNITS": v} for k, v in sorted(planned.items())]
    )


def _model_bom(data) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "MODEL_ID": r["MODEL_ID"],
                "FG_PART_ID": r["FG_PART_ID"],
                "QTY_PER_VEHICLE": r["QTY_PER_VEHICLE"],
            }
            for r in data["dim_model_bom"]
            if r.get("IS_CURRENT", True)
        ]
    )


def _bom(data) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PARENT_PART_ID": r["fg_part_id"],
                "CHILD_PART_ID": r["component_part_id"],
                "QTY_PER_UNIT": r["qty_per_unit"],
            }
            for r in data["dim_bom"]
        ]
    )
