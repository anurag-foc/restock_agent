"""Supplier performance facts — `fact_supplier_delivery` and `fact_supplier_quality`.

Both are built from the receipt events the reorder loop already produced, so every recorded
delivery is an event that genuinely moved stock. The delivery history and the inventory history
describe one world.

**Data capture is deliberately incomplete, and that is the point.** The loop generates ~41,000
receipts; recording all of them would give every (supplier, part) pair a rich history, and E2's
hierarchical fallback — (supplier, part) → (supplier) → (category) → contracted — would never
leave its first tier. So each contracted pair is assigned a *capture tier* and only that many of
its receipts get a delivery record. This is not a shortcut: patchy delivery-performance capture
is exactly the stale-master-data reality docs/market_evidence_phase1.md §4 describes, and it is
the reason a fallback hierarchy has to exist at all.

Finding-bearing suppliers always get rich capture. A finding that cannot be detected because we
withheld its evidence would be indistinguishable from a detector failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agentic_restock.generation import contracts, dates, demand, entities
from agentic_restock.generation.replenishment import PairHistory, ReceiptEvent
from agentic_restock.generation.scenarios import FINDINGS

# How many receipts get a delivery record, per capture tier. "rich" is capped rather than
# unlimited: 40 deliveries over three years is ample for any estimator, and recording every one
# of ~150 per pair buys nothing but rows.
CAPTURE_TIERS = {
    "rich": 40,
    "medium": 4,
    "sparse": 2,
    "none": 0,
}

# Freight per delivery, as a fraction of the consignment's value. Populates FREIGHT_COST, which
# exists on the table but has never carried data -- and which FX1 needs to quote a transfer's
# real cost instead of describing the downside in words.
FREIGHT_RATE_OF_VALUE = 0.015
FREIGHT_MINIMUM = 1800.0

DAMAGE_SHARE_OF_DEFECTS = 0.4


@dataclass
class DeliveryRecord:
    event: ReceiptEvent
    planned_date_key: int
    delivery_date_key: int
    delay_days: int
    quantity: int
    freight_cost: float
    damaged_qty: int
    short_qty: int
    otd_flag: bool


def capture_tiers() -> dict[tuple[str, str], str]:
    """(part_id, supplier_id) -> capture tier.

    Assigned by sorted position so the mix is deterministic, after pinning the pairs whose
    findings depend on having evidence.
    """
    pinned: dict[tuple[str, str], str] = {}

    f4 = FINDINGS["F4"]
    for part_id in f4["parts"]:
        supplier = contracts.preferred_supplier(part_id)
        if supplier:
            pinned[(part_id, supplier)] = "rich"

    for supplier_id, _price in FINDINGS["F5"]["suppliers"]:
        pinned[(FINDINGS["F5"]["part"], supplier_id)] = "rich"

    # The improving archetype needs enough history for recency weighting to have something to
    # weigh -- a handful of records cannot distinguish "was late, now fine" from "is late".
    for row in contracts.contracts():
        if contracts.archetype_of(row["supplier_id"]) == "improving":
            pinned.setdefault((row["part_id"], row["supplier_id"]), "rich")

    # The `untested` archetype gets NO capture on ANY of its parts. Assigning it a rotating tier
    # left its supplier-level pool populated, so E2's ladder never fell past the supplier tier
    # and the `contracted` tier -- the "we have never measured this supplier" case -- was
    # unreachable in the whole dataset. An archetype named untested has to mean untested.
    for row in contracts.contracts():
        if contracts.archetype_of(row["supplier_id"]) == "untested":
            pinned[(row["part_id"], row["supplier_id"])] = "none"

    rotation = ["medium", "sparse", "none", "sparse", "medium", "rich"]
    assignment = dict(pinned)
    index = 0
    for row in sorted(contracts.contracts(), key=lambda r: (r["part_id"], r["supplier_id"])):
        key = (row["part_id"], row["supplier_id"])
        if key in assignment:
            continue
        assignment[key] = rotation[index % len(rotation)]
        index += 1
    return assignment


def _captured_events(
    histories: dict[tuple[str, str], PairHistory], limit: int, part_id: str
) -> list[tuple[ReceiptEvent, int]]:
    """The most recent `limit` landed receipts for a part, pooled ACROSS its warehouses.

    Pooling matters: E2 fits at (supplier, part), so a cap applied per (part, warehouse) is
    multiplied by however many warehouses stock the part. That put 160 deliveries on pairs whose
    capture tier allows 40, and every tier collapsed into "rich".
    """
    landed: list[tuple[ReceiptEvent, int]] = []
    for (candidate_part, _warehouse), history in histories.items():
        if candidate_part != part_id:
            continue
        n = len(history.issues)
        landed.extend(
            (event, n)
            for event in history.events
            if event.arrival_day < n and event.quantity > 0
        )
    landed.sort(key=lambda pair: pair[0].arrival_day)
    return landed[-limit:] if limit else []


def delivery_records(histories: dict[tuple[str, str], PairHistory]) -> list[DeliveryRecord]:
    """One record per captured receipt, with planned vs actual dates and the resulting delay."""
    tiers = capture_tiers()
    records: list[DeliveryRecord] = []
    params_by_part = {part: h.params for (part, _wh), h in histories.items()}

    for part_id, params in sorted(params_by_part.items()):
        supplier_id = contracts.preferred_supplier(part_id)
        if supplier_id is None:
            continue
        tier = tiers.get((part_id, supplier_id), "none")
        limit = CAPTURE_TIERS[tier]
        if not limit:
            continue

        contracted = contracts.contracted_lead_days(part_id, supplier_id) or 30

        for event, n in _captured_events(histories, limit, part_id):
            # Planned = what the contract promised from the order date. Actual = when it landed.
            # The difference is the observable that nuance 5 works from.
            planned_day = event.order_day + contracted
            delay = event.arrival_day - planned_day

            rng = demand._rng(
                event.part_id, event.warehouse_id, "delivery", event.order_day
            )
            defects = rng.binomial(event.quantity, contracts.reject_rate(supplier_id))
            damaged = round(defects * DAMAGE_SHARE_OF_DEFECTS)
            value = event.quantity * params.unit_cost  # params: any warehouse, same part cost

            records.append(
                DeliveryRecord(
                    event=event,
                    planned_date_key=dates.day_index_key(min(planned_day, n - 1), n)
                    if planned_day >= 0
                    else dates.day_index_key(0, n),
                    delivery_date_key=dates.day_index_key(event.arrival_day, n),
                    delay_days=delay,
                    quantity=event.quantity,
                    freight_cost=round(
                        max(value * FREIGHT_RATE_OF_VALUE, FREIGHT_MINIMUM), 2
                    ),
                    damaged_qty=damaged,
                    short_qty=max(defects - damaged, 0),
                    otd_flag=delay <= 0,
                )
            )

    return records


def delivery_rows(records: list[DeliveryRecord]) -> list[dict]:
    """`fact_supplier_delivery` rows, including the newly added PART_KEY."""
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    warehouse_keys = {w["WAREHOUSE_ID"]: w["WAREHOUSE_KEY"] for w in entities.warehouses()}
    supplier_keys = {s["SUPPLIER_ID"]: s["SUPPLIER_KEY"] for s in entities.suppliers()}

    rows: list[dict] = []
    for index, record in enumerate(sorted(records, key=lambda r: r.delivery_date_key), start=1):
        event = record.event
        rows.append(
            {
                "SUPPLIER_DELIVERY_KEY": index,
                "DELIVERY_ID": f"DEL-{index:07d}",
                "DELIVERY_DATE_KEY": record.delivery_date_key,
                "PLANNED_DATE_KEY": record.planned_date_key,
                "SUPPLIER_KEY": supplier_keys[event.supplier_id],
                "WAREHOUSE_KEY": warehouse_keys[event.warehouse_id],
                "PART_KEY": part_keys[event.part_id],
                "QUANTITY": record.quantity,
                "DELAY_DAYS": record.delay_days,
                "DAMAGED_QTY": record.damaged_qty,
                "SHORT_QTY": record.short_qty,
                "FREIGHT_COST": record.freight_cost,
                "OTD_FLAG": record.otd_flag,
                "DELIVERY_STATUS": "RECEIVED",
                "DW_SOURCE": entities.DW_SOURCE,
            }
        )
    return rows


def quality_rows(records: list[DeliveryRecord]) -> list[dict]:
    """`fact_supplier_quality` rows — one inspection per captured delivery.

    Every measure column here is currently NULL on all 1100 rows in the replica, which is why
    the supplier-economics detector has nothing to price. `effective_unit_cost` needs a defect
    rate, and a defect rate needs INSPECTED_QTY and DEFECT_QTY to be real numbers.
    """
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    supplier_keys = {s["SUPPLIER_ID"]: s["SUPPLIER_KEY"] for s in entities.suppliers()}
    unit_costs = {p["PART_ID"]: float(p["UNIT_COST"]) for p in entities.parts()}

    rows: list[dict] = []
    for index, record in enumerate(sorted(records, key=lambda r: r.delivery_date_key), start=1):
        event = record.event
        defects = record.damaged_qty + record.short_qty
        inspected = record.quantity
        ppm = round(defects / inspected * 1_000_000) if inspected else 0
        score = round(max(0.0, 100.0 * (1.0 - defects / inspected)), 2) if inspected else 100.0
        rows.append(
            {
                "SUPPLIER_QUALITY_KEY": index,
                "QUALITY_ID": f"QI-{index:07d}",
                "INSPECTION_DATE_KEY": record.delivery_date_key,
                "SUPPLIER_KEY": supplier_keys[event.supplier_id],
                "PART_KEY": part_keys[event.part_id],
                "INSPECTOR_EMPLOYEE_KEY": -1,
                "INSPECTED_QTY": inspected,
                "DEFECT_QTY": defects,
                "QUALITY_SCORE": score,
                "PPM_LEVEL": ppm,
                "COST_OF_POOR_QUALITY": round(defects * unit_costs[event.part_id], 2),
                "DISPOSITION": "SORT_AND_USE" if defects else "ACCEPT",
                "LINE_REJECTION_FLAG": defects > 0 and record.short_qty > 0,
                "DW_SOURCE": entities.DW_SOURCE,
            }
        )
    return rows


def observed_lead_summary(records: list[DeliveryRecord]) -> dict[tuple[str, str], dict]:
    """Realised delay statistics per (part, supplier) — what E2 should be able to recover.

    Recorded into ground truth alongside the *true* archetype parameters. The two differ: the
    estimator can only ever recover what the captured sample shows, so a gate that compared it
    against the true parameters would fail sparse-capture pairs for the wrong reason.
    """
    grouped: dict[tuple[str, str], list[int]] = {}
    for record in records:
        key = (record.event.part_id, record.event.supplier_id)
        grouped.setdefault(key, []).append(record.delay_days)

    summary: dict[tuple[str, str], dict] = {}
    for key, delays in grouped.items():
        arr = np.array(delays, dtype=float)
        summary[key] = {
            "n": len(arr),
            "observed_mean_delay": float(arr.mean()),
            "observed_sigma_delay": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
            "otd_rate": float((arr <= 0).mean()),
        }
    return summary
