"""Inventory facts — `fact_inventory_snapshot` and `fact_inventory_transaction`.

Transactions carry **full daily fidelity**: they are the burn estimator's only input, and
thinning them would degrade the very thing Phase 2 measures.

Snapshots are **sampled**: daily for the recent window, month-end before that. 278 pairs x 1100
days is 293,600 rows, and nothing needs them all — the detectors read the *latest* snapshot per
pair, and history comes from transactions. Sampling costs nothing that matters and keeps the
load an order of magnitude smaller.

Crucially it preserves the audit property. Reconciliation is
`on_hand[t2] - on_hand[t1] == receipts - issues` over `(t1, t2]`, which holds between *any* two
sampled dates, not just consecutive days. So someone can still check the snapshot series against
the transaction series and have it tie out exactly.
"""

from __future__ import annotations

import numpy as np

from agentic_restock.generation import dates, entities
from agentic_restock.generation.replenishment import PairHistory

# Daily detail for the recent window; month-end only before it.
DAILY_WINDOW_DAYS = 120

TXN_ISSUE = "ISSUE"
TXN_RECEIPT = "RECEIPT"

# Stock the risk model cannot draw on. Kept small and non-zero so a detector that ignores
# ALLOCATED_QTY and one that subtracts it produce visibly different answers.
ALLOCATED_FRACTION = 0.04
BLOCKED_FRACTION = 0.01


def _sampled_days(series_length: int) -> list[int]:
    """Day indexes to emit a snapshot for: every day recently, month-ends further back."""
    recent_start = max(0, series_length - DAILY_WINDOW_DAYS)
    days = list(range(recent_start, series_length))

    for day in range(recent_start):
        current = dates.date_for(day, series_length)
        following = dates.date_for(day + 1, series_length)
        if current.month != following.month:  # last day of its month
            days.append(day)

    return sorted(set(days))


def snapshot_rows(histories: dict[tuple[str, str], PairHistory]) -> list[dict]:
    """`fact_inventory_snapshot` — a daily fact, so one row per (part, warehouse, sampled date)."""
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    warehouse_keys = {w["WAREHOUSE_ID"]: w["WAREHOUSE_KEY"] for w in entities.warehouses()}

    rows: list[dict] = []
    key = 1

    for (part_id, warehouse_id), history in sorted(histories.items()):
        params = history.params
        n = len(history.issues)
        # Trailing average consumption, written as the NAIVE flat mean on purpose: it is the
        # number the old board used, so a detector that reaches for it instead of the corrected
        # forward burn is visibly worse rather than merely different.
        trailing = history.issues[-365:] if n >= 365 else history.issues
        flat_mean = float(trailing.mean())

        in_transit_today = history.in_transit_qty

        for day in _sampled_days(n):
            on_hand = int(history.on_hand[day])
            allocated = round(on_hand * ALLOCATED_FRACTION)
            blocked = round(on_hand * BLOCKED_FRACTION)
            available = max(on_hand - allocated - blocked, 0)
            is_today = day == n - 1

            days_of_supply = round(on_hand / flat_mean, 1) if flat_mean > 0 else None
            rows.append(
                {
                    "INVENTORY_SNAPSHOT_KEY": key,
                    "STOCK_ID": f"ST-{part_id}-{warehouse_id}-{day:04d}",
                    "SNAPSHOT_DATE_KEY": dates.day_index_key(day, n),
                    "PART_KEY": part_keys[part_id],
                    "WAREHOUSE_KEY": warehouse_keys[warehouse_id],
                    "QUANTITY_ON_HAND": on_hand,
                    "SAFETY_STOCK_QTY": params.safety_stock,
                    "MAX_STOCK_LEVEL": params.max_stock,
                    "ALLOCATED_QTY": allocated,
                    "AVAILABLE_QTY": available,
                    # Only meaningful as of today; historical in-transit is not reconstructed.
                    "IN_TRANSIT_QTY": in_transit_today if is_today else 0,
                    "BLOCKED_QTY": blocked,
                    "DAYS_OF_SUPPLY": days_of_supply,
                    "AVG_DAILY_CONSUMPTION": round(flat_mean, 2),
                    "INVENTORY_TURNOVER_RATIO": (
                        round(flat_mean * 365 / on_hand, 2) if on_hand > 0 else None
                    ),
                    "STOCK_VALUATION": round(on_hand * params.unit_cost, 2),
                    "STOCKOUT_RISK": _stockout_risk(on_hand, params.safety_stock),
                    "DW_SOURCE": entities.DW_SOURCE,
                }
            )
            key += 1

    return rows


def _stockout_risk(on_hand: int, safety_stock: int) -> str:
    """The source system's own crude label — retained because it is what the data really carries.

    Deliberately a threshold band on on-hand alone. It ignores burn rate, lead time and
    variability, which is precisely why the redesign computes its own probability instead of
    reading this column.
    """
    if safety_stock <= 0:
        return "UNKNOWN"
    ratio = on_hand / safety_stock
    if ratio < 0.5:
        return "CRITICAL"
    if ratio < 1.0:
        return "HIGH"
    if ratio < 1.5:
        return "MEDIUM"
    return "LOW"


def transaction_rows(histories: dict[tuple[str, str], PairHistory]) -> list[dict]:
    """`fact_inventory_transaction` — ISSUE rows for consumption, RECEIPT rows for arrivals.

    Full daily fidelity: this is the burn estimator's only input.
    """
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    warehouse_keys = {w["WAREHOUSE_ID"]: w["WAREHOUSE_KEY"] for w in entities.warehouses()}
    plant_lines = {
        plant["PLANT_ID"]: [
            line["LINE_KEY"]
            for line in entities.production_lines()
            if line["PLANT_ID"] == plant["PLANT_ID"]
        ]
        for plant in entities.plants()
    }
    warehouse_plants = {
        w["WAREHOUSE_ID"]: w.get("LINKED_PLANT_ID") for w in entities.warehouses()
    }

    rows: list[dict] = []
    key = 1

    for (part_id, warehouse_id), history in sorted(histories.items()):
        params = history.params
        n = len(history.issues)
        part_key = part_keys[part_id]
        warehouse_key = warehouse_keys[warehouse_id]
        plant_id = warehouse_plants.get(warehouse_id)
        lines = plant_lines.get(plant_id or "", [])

        for day in np.nonzero(history.issues)[0]:
            day = int(day)
            quantity = int(history.issues[day])
            # Consumption at a plant store is a line issue against a production order; an RDC
            # issue is an aftermarket pick with no line behind it.
            line_key = lines[day % len(lines)] if lines else None
            rows.append(
                _txn_row(
                    key,
                    TXN_ISSUE,
                    day,
                    n,
                    part_key,
                    warehouse_key,
                    quantity,
                    params.unit_cost,
                    int(history.on_hand[day]),
                    line_key,
                    f"PO-{dates.day_index_key(day, n)}-{warehouse_id}" if line_key else None,
                )
            )
            key += 1

        for day in np.nonzero(history.receipts)[0]:
            day = int(day)
            quantity = int(history.receipts[day])
            rows.append(
                _txn_row(
                    key,
                    TXN_RECEIPT,
                    day,
                    n,
                    part_key,
                    warehouse_key,
                    quantity,
                    params.unit_cost,
                    int(history.on_hand[day]),
                    None,
                    None,
                )
            )
            key += 1

    return rows


def _txn_row(
    key: int,
    txn_type: str,
    day: int,
    series_length: int,
    part_key: int,
    warehouse_key: int,
    quantity: int,
    unit_cost: float,
    balance_after: int,
    line_key: int | None,
    production_order_id: str | None,
) -> dict:
    return {
        "INVENTORY_TXN_KEY": key,
        "TRANSACTION_ID": f"TX-{key:09d}",
        "TRANSACTION_DATE_KEY": dates.day_index_key(day, series_length),
        "PART_KEY": part_key,
        "WAREHOUSE_KEY": warehouse_key,
        "PRODUCTION_ORDER_ID": production_order_id,
        "LINE_KEY": line_key,
        "OPERATOR_EMPLOYEE_KEY": -1,
        "TRANSACTION_TYPE": txn_type,
        "QUANTITY": quantity,
        "UNIT_COST": round(unit_cost, 2),
        "TRANSACTION_VALUE": round(quantity * unit_cost, 2),
        "BALANCE_AFTER_TXN": balance_after,
        "DW_SOURCE": entities.DW_SOURCE,
    }
