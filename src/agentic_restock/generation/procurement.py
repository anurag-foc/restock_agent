"""Open purchase orders — `fact_procurement`.

This table's role here is the **inbound pipeline**, nothing more: `PENDING_QTY` plus
`EXPECTED_DATE_KEY` plus `STATUS` is exactly `on_order_arriving_within_lead`, which the risk
model needs in order to know that a low balance with stock already inbound is a waiting problem
rather than a shortage.

It deliberately does **not** serve lead-time history. `fact_procurement` has no actual-receipt
date column — only `ORDER_DATE_KEY` and `EXPECTED_DATE_KEY` — so it can measure a *promise* and
never a *performance*. Observed lead time comes from `fact_supplier_delivery` instead. See
docs/schema_changes_gold_dev_analytics.md §1.1; that gap is an open request to Data Engineering,
and `docs/market_evidence_phase1.md` §4 previously claimed the opposite.

Rows come from the receipt events still in transit today, so an open PO and the in-transit
quantity the risk model sees are the same object rather than two independent inventions.
"""

from __future__ import annotations

from agentic_restock.generation import contracts, dates, entities
from agentic_restock.generation.replenishment import PairHistory

GST_RATE = 0.18


def open_po_rows(histories: dict[tuple[str, str], PairHistory]) -> list[dict]:
    """One row per order placed but not yet arrived."""
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    supplier_keys = {s["SUPPLIER_ID"]: s["SUPPLIER_KEY"] for s in entities.suppliers()}
    plant_keys = {p["PLANT_ID"]: p["PLANT_KEY"] for p in entities.plants()}
    warehouses = {w["WAREHOUSE_ID"]: w for w in entities.warehouses()}
    warehouse_keys = {w["WAREHOUSE_ID"]: w["WAREHOUSE_KEY"] for w in entities.warehouses()}
    default_plant = entities.plants()[0]["PLANT_KEY"]

    rows: list[dict] = []
    key = 1

    for (part_id, warehouse_id), history in sorted(histories.items()):
        n = len(history.issues)
        supplier_id = history.events[0].supplier_id if history.events else None
        if supplier_id is None:
            continue

        contract = contracts.contract_for(part_id, supplier_id)
        unit_rate = float(contract["unit_cost"]) if contract else history.params.unit_cost
        contracted_lead = contract["lead_time_days"] if contract else 30

        linked_plant = warehouses[warehouse_id].get("LINKED_PLANT_ID")
        plant_key = plant_keys.get(linked_plant, default_plant) if linked_plant else default_plant

        for event in history.events:
            if event.arrival_day < n:
                continue  # already landed; it is a delivery, not an open PO

            expected_day = event.order_day + contracted_lead
            total = round(event.quantity * unit_rate, 2)
            rows.append(
                {
                    "PROCUREMENT_KEY": key,
                    "PURCHASE_ORDER_ID": f"PO-{key:07d}",
                    "ORDER_DATE_KEY": dates.day_index_key(event.order_day, n),
                    # Clamped into the calendar the date dimension covers; an order placed near
                    # the end of the window can promise beyond it.
                    "EXPECTED_DATE_KEY": dates.day_index_key(
                        min(expected_day, n + 400), n
                    ),
                    "PART_KEY": part_keys[part_id],
                    "SUPPLIER_KEY": supplier_keys[supplier_id],
                    "PLANT_KEY": plant_key,
                    # The destination, which this loop knows exactly and used to throw away.
                    # `PLANT_KEY` cannot carry it: a regional DC has no linked plant, so every
                    # DC's orders fell back to the default plant and the position query's
                    # plant-link join delivered them to that plant's store instead. Every DC
                    # read as having no inbound stock at all, and one plant store read as
                    # having the whole network's -- which is a supply position no MRP would
                    # recognise. `build_current_position_query` already prefers this column
                    # (`has_warehouse_key` defaults to True); it simply was never written.
                    "WAREHOUSE_KEY": warehouse_keys[warehouse_id],
                    "BUYER_EMPLOYEE_KEY": -1,
                    "PO_TYPE": "SCHEDULED",
                    "STATUS": "ISSUED",
                    "ORDER_QTY": event.quantity,
                    "UNIT_RATE": round(unit_rate, 2),
                    "TOTAL_AMOUNT": total,
                    "RECEIVED_QTY": 0,
                    "PENDING_QTY": event.quantity,
                    "TAX_AMOUNT": round(total * GST_RATE, 2),
                    "DW_SOURCE": entities.DW_SOURCE,
                }
            )
            key += 1

    return rows


def inbound_by_pair(rows: list[dict]) -> dict[int, int]:
    """PART_KEY -> total pending quantity. A cross-check that the pipeline is non-empty."""
    totals: dict[int, int] = {}
    for row in rows:
        totals[row["PART_KEY"]] = totals.get(row["PART_KEY"], 0) + row["PENDING_QTY"]
    return totals
