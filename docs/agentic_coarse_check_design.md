# Agentic Coarse Check — Design Note

**Status:** Planned — not yet implemented  
**Related file:** `notebooks/lakeflow_trigger/coarse_check.py`  
**Replaces:** Static `QUANTITY_ON_HAND <= SAFETY_STOCK_QTY` threshold filter

---

## The Problem

The current Lakeflow coarse check is a single SQL filter:

```sql
WHERE QUANTITY_ON_HAND <= SAFETY_STOCK_QTY
```

This is a **threshold alerter**. It fires only *after* stock has already fallen into the danger zone — which means:

- It is **reactive**, not predictive. If a supplier takes 7 days to deliver, the system is already 7 days late when it fires.
- It fires on a **symptom** (low stock number), not on a **reason** (consumption velocity, lead-time mismatch, BOM cascade risk).
- The Supervisor Agent receives a candidate list with no context — it has to reverse-engineer *why* the alert fired before it can reason about it.

The result: the Genie Space and Supervisor Agent may be intelligence-first, but the system's **entry point** is still a threshold alerter. Garbage in, rationalized output.

---

## The Design: Multi-Signal Agentic Scanner

Replace the single threshold filter with a **three-signal UNION query** that uses our deployed UC functions to surface candidates by *reason*, not just by raw stock level.

### Signal 1 — Stock Threshold (keep, existing behavior)

```sql
SELECT 
    p.PART_ID,
    w.WAREHOUSE_ID,
    'STOCK_THRESHOLD'      AS signal_type,
    'CRITICAL'             AS initial_urgency,
    s.QUANTITY_ON_HAND     AS current_stock_qty,
    s.SAFETY_STOCK_QTY     AS reorder_point_qty,
    NULL                   AS days_to_stockout,
    NULL                   AS threatened_assembly
FROM gold_dev.supply_chain_analytics.fact_inventory_snapshot s
JOIN gold_dev.dim.dim_part      p ON s.PART_KEY      = p.PART_KEY
JOIN gold_dev.dim.dim_warehouse w ON s.WAREHOUSE_KEY = w.WAREHOUSE_KEY
WHERE s.QUANTITY_ON_HAND <= s.SAFETY_STOCK_QTY
```

**What it detects:** Stock has already fallen to or below the safety buffer.  
**Why it stays:** This is still a valid, urgent signal. We keep it as Signal 1 because if the stock is already below safety stock, the Supervisor Agent needs to know and act fast.

---

### Signal 2 — Predictive Stockout (new)

```sql
SELECT 
    p.PART_ID,
    w.WAREHOUSE_ID,
    'PREDICTED_STOCKOUT'   AS signal_type,
    'HIGH'                 AS initial_urgency,
    s.QUANTITY_ON_HAND     AS current_stock_qty,
    s.SAFETY_STOCK_QTY     AS reorder_point_qty,
    DATEDIFF(
        gold_dev.supply_chain_analytics.predicted_stockout_date(p.PART_ID, w.WAREHOUSE_ID),
        CURRENT_DATE
    )                      AS days_to_stockout,
    NULL                   AS threatened_assembly
FROM gold_dev.supply_chain_analytics.fact_inventory_snapshot s
JOIN gold_dev.dim.dim_part      p ON s.PART_KEY      = p.PART_KEY
JOIN gold_dev.dim.dim_warehouse w ON s.WAREHOUSE_KEY = w.WAREHOUSE_KEY
-- Not already flagged by Signal 1
WHERE s.QUANTITY_ON_HAND > s.SAFETY_STOCK_QTY
-- But will stockout before a replacement order can arrive
  AND gold_dev.supply_chain_analytics.predicted_stockout_date(p.PART_ID, w.WAREHOUSE_ID)
      <= DATE_ADD(CURRENT_DATE,
             CAST(gold_dev.supply_chain_analytics.dynamic_reorder_point(
                       p.PART_ID, w.WAREHOUSE_ID,
                       (SELECT MAX(s2.SUPPLIER_ID)
                        FROM gold_dev.supply_chain_analytics.dim_supplier_contract s2
                        WHERE s2.PART_ID = p.PART_ID
                        ORDER BY s2.LEAD_TIME_DAYS ASC LIMIT 1)
                  ) AS INT)
         )
```

**What it detects:** The part looks fine in today's snapshot — stock is above safety stock — but at the current burn rate it will stockout *before* an order placed today could even arrive.

**How each function contributes:**
- `predicted_stockout_date(part_id, warehouse_id)` → projects the exact calendar date stock hits zero based on trailing consumption.
- `dynamic_reorder_point(part_id, warehouse_id, supplier_id)` → returns the number of lead-time days needed for the preferred supplier to deliver.
- The condition: if the projected stockout date ≤ today + lead-time days, act now — you're already in the risk window even though the snapshot looks OK.

**Why this matters:** This is the signal that turns the system proactive. The Supervisor Agent gets called *before* the threshold is breached, while there is still time to act.

---

### Signal 3 — BOM Cascade Risk (new)

```sql
SELECT
    bom.COMPONENT_PART_ID  AS part_id,
    snap.WAREHOUSE_ID,
    'BOM_CASCADE_RISK'     AS signal_type,
    'HIGH'                 AS initial_urgency,
    snap.QUANTITY_ON_HAND  AS current_stock_qty,
    snap.SAFETY_STOCK_QTY  AS reorder_point_qty,
    NULL                   AS days_to_stockout,
    fg.PART_ID             AS threatened_assembly
FROM gold_dev.supply_chain_analytics.dim_bom bom
JOIN gold_dev.dim.dim_part fg
    ON bom.FG_PART_ID = fg.PART_ID
   AND fg.CRITICALITY_CLASS = 'A-CRITICAL'       -- only cascade from critical finished goods
JOIN gold_dev.supply_chain_analytics.fact_inventory_snapshot snap
    ON snap.PART_ID = bom.COMPONENT_PART_ID
-- Component has insufficient stock for the forecasted production run
WHERE (snap.QUANTITY_ON_HAND - (bom.QTY_PER_UNIT * fg.FORECAST_QTY)) < 0
-- Not already flagged by Signals 1 or 2
  AND snap.QUANTITY_ON_HAND > snap.SAFETY_STOCK_QTY
```

**What it detects:** A sub-component (e.g. Bearing B-220) is not yet below its own safety stock, so Signal 1 and Signal 2 would never fire for it. But the production plan for a critical finished good (e.g. Engine A) demands more of this component than is currently available — creating a BOM cascade risk.

**Why this matters:** A ₹50 spring clip can halt assembly of a ₹50,000 engine. Without this signal, the system would only discover the bottleneck reactively — when the component finally crosses its own safety stock days or weeks later. By then, the assembly line may already be stopped.

---

## The Full UNION: Candidate List with Signal Context

```sql
-- Signal 1 UNION Signal 2 UNION Signal 3
-- Deduplicated: if the same part/warehouse appears in multiple signals,
-- take the highest urgency signal only.
SELECT part_id, warehouse_id, signal_type, initial_urgency,
       current_stock_qty, reorder_point_qty, days_to_stockout, threatened_assembly
FROM (
    <Signal 1 query>
    UNION ALL
    <Signal 2 query>
    UNION ALL
    <Signal 3 query>
)
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY part_id, warehouse_id
    ORDER BY CASE initial_urgency WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END
) = 1
```

---

## What the Supervisor Agent Receives

**Before (threshold only):**
```json
[
  { "item_id": "P1003", "warehouse_id": "WH003", "current_stock_qty": 110, "reorder_point_qty": 200 }
]
```

**After (signal-annotated):**
```json
[
  {
    "part_id": "P1003", "warehouse_id": "WH003",
    "signal_type": "STOCK_THRESHOLD", "initial_urgency": "CRITICAL",
    "current_stock_qty": 110, "reorder_point_qty": 200,
    "days_to_stockout": null, "threatened_assembly": null
  },
  {
    "part_id": "P1006", "warehouse_id": "WH009",
    "signal_type": "PREDICTED_STOCKOUT", "initial_urgency": "HIGH",
    "current_stock_qty": 160, "reorder_point_qty": 150,
    "days_to_stockout": 6, "threatened_assembly": null
  },
  {
    "part_id": "BEARING-B220", "warehouse_id": "WH003",
    "signal_type": "BOM_CASCADE_RISK", "initial_urgency": "HIGH",
    "current_stock_qty": 850, "reorder_point_qty": 500,
    "days_to_stockout": null, "threatened_assembly": "ENGINE-A"
  }
]
```

---

## Supervisor Agent Routing by Signal Type

The Supervisor Agent's system prompt should route each signal type through a different analytical path:

| `signal_type` | Supervisor Routing |
|---|---|
| `STOCK_THRESHOLD` | Full 4-layer analysis: Validate Signal → Procurement Options → Manufacturing → Financial |
| `PREDICTED_STOCKOUT` | Emphasize lead-time scenarios from `dynamic_reorder_point`; surface fallback supplier option |
| `BOM_CASCADE_RISK` | Lead with `assembly_risk_report` and BOM explosion; then restock the component |

---

## Implementation Plan (Option A — Enrich existing check)

1. **Update `notebooks/lakeflow_trigger/coarse_check.py`**:
   - Replace the current single `WHERE QUANTITY_ON_HAND <= SAFETY_STOCK_QTY` filter with the 3-signal UNION query.
   - Add `signal_type`, `initial_urgency`, `days_to_stockout`, `threatened_assembly` to the candidate JSON payload.

2. **Update `notebooks/lakeflow_trigger/invoke_supervisor.py`**:
   - Update the prompt sent to the Supervisor Agent to include signal context.
   - Supervisor prompt prefix changes from *"these parts are below their reorder point"* to *"here are the signals detected and why each was flagged"*.

3. **Update `SUPERVISOR_INSTRUCTIONS` in `scripts/create_supervisor_agent.py`**:
   - Add routing rules for each `signal_type` so the Supervisor knows which intelligence layer to lead with per signal.

---

## Open Questions

| # | Question |
|---|---|
| 1 | Does `dim_bom` have sufficient data seeded for Signal 3 to fire? Need to verify row count. |
| 2 | Is `FORECAST_QTY` available on `dim_part`, or do we need a separate production plan table? |
| 3 | Signal 2's `dynamic_reorder_point` requires a `supplier_id` — the query uses the lowest-lead-time supplier as a default. Is preferred supplier preference stored anywhere? |
