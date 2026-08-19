# Unity Catalog Functions Reference — `ab_training.agentic_restock`

This is the function-level reference for the §4.2 deep-analysis logic the
Genie Agent uses. It complements `docs/architecture.md` (the workflow/data
model design) and `docs/agent_bricks_mapping.md` (how these functions map to
Databricks Agent Bricks primitives) — this doc is a lookup table for "what
does this function do, when do I call it, what does it return."

**Source of truth:** `notebooks/uc_functions/deep_analysis_functions.ipynb`
(deployed via the on-demand `deploy_uc_functions` job). If this doc and the
notebook ever disagree, the notebook — and the live function's own `COMMENT`,
queryable via `DESCRIBE FUNCTION EXTENDED <name>` — wins.

**Consumers:** all 9 functions are registered as trusted `sql_functions` on
the Genie Space (`notebooks/genie/genie_agent.geniespace.json`). The
Supervisor Agent has **no direct access** to any of them — it only reaches
this logic by asking the Genie Agent a natural-language question (see
`docs/agent_bricks_mapping.md` §2.4 for why).

---

## At a glance

| # | Function | Kind | Returns | One-line use case |
|---|---|---|---|---|
| 1 | [`avg_daily_consumption`](#1-avg_daily_consumption) | scalar | `DOUBLE` | "How fast is this part being used?" |
| 2 | [`predicted_stockout_date`](#2-predicted_stockout_date) | scalar | `DATE` | "When will this part run out?" |
| 3 | [`classify_urgency`](#3-classify_urgency) | scalar (pure) | `STRING` | "How urgent is this candidate?" |
| 4 | [`requested_restock_qty`](#4-requested_restock_qty) | scalar | `INT` | "How many units should we order?" |
| 5 | [`pending_procurement_qty`](#5-pending_procurement_qty) | scalar | `DOUBLE` | "Is a purchase order already covering this?" (veto input, the number) |
| 6 | [`open_procurement_orders`](#6-open_procurement_orders) | table-valued | rows | "Which specific PO(s) are covering this?" (veto input, the detail) |
| 7 | [`restock_candidate_summary`](#7-restock_candidate_summary) | scalar | `STRING` | Canned "why does X need restocking" paragraph |
| 8 | [`avg_lead_time_days`](#8-avg_lead_time_days) | scalar | `DOUBLE` | "How long does this part usually take to arrive once ordered?" |
| 9 | [`latest_snapshot`](#9-latest_snapshot) | table-valued | 1 row | "What's the current stock position for this part/warehouse?" |

All functions take `part_id` (business key, e.g. `P1001`) and, except
`avg_lead_time_days`, `warehouse_id` (business key, e.g. `WH001`) — never the
`gold_dev` surrogate keys (`PART_KEY`/`WAREHOUSE_KEY`).

> **No `needs_restock` boolean function.** The restock veto (architecture
> §4.2) is Genie's own reasoning, not a single yes/no call — see
> [Composing the restock veto](#composing-the-restock-veto) below.

---

## 1. `avg_daily_consumption`

```sql
avg_daily_consumption(part_id STRING, warehouse_id STRING, lookback_days INT DEFAULT 14) RETURNS DOUBLE
```

**What it computes:** trailing-window average daily consumption, anchored to
today. Sums `QUANTITY` over `ISSUE`-type rows in
`gold_dev.supply_chain_analytics.fact_inventory_transaction` in the trailing
`lookback_days` window, divided by `lookback_days`.

**Use case:** the input to almost everything else — "how fast is this part
moving?" Also useful standalone for a consumption-trend question, or with a
non-default window (e.g. `lookback_days = 30`) to compare a shorter vs.
longer trend.

**Edge cases:** returns `0.0` (not `NULL`) if there's no consumption in the
window — a part with zero recent usage still gets a valid, usable number
downstream (`predicted_stockout_date` treats `0.0` as "unforecastable" rather
than dividing by zero).

**Example:**
```sql
SELECT ab_training.agentic_restock.avg_daily_consumption('P1003', 'WH001', 14) AS avg_daily_consumption
```

---

## 2. `predicted_stockout_date`

```sql
predicted_stockout_date(part_id STRING, warehouse_id STRING) RETURNS DATE
```

**What it computes:** `today + CEIL(latest QUANTITY_ON_HAND / avg_daily_consumption(part_id, warehouse_id, 14))`.
Takes the latest `fact_inventory_snapshot` row (deduped via `MAX_BY(...,
SNAPSHOT_DATE_KEY)`) for on-hand stock.

**Use case:** "when will this part run out?" — the forecast half of urgency
scoring, and a common standalone question ("When is the 12V 60Ah Exide
Battery at WH-KOL predicted to run out of stock?").

**Edge cases:** returns `NULL` when `avg_daily_consumption` is `~0` — there's
nothing to forecast, not an error. `classify_urgency` treats a `NULL`
`days_remaining` as `LOW` (unless the snapshot's own `STOCKOUT_RISK` signal
says otherwise).

**Example:**
```sql
SELECT ab_training.agentic_restock.predicted_stockout_date('P1005', 'WH005') AS predicted_stockout_date
```

---

## 3. `classify_urgency`

```sql
classify_urgency(stockout_risk STRING, days_remaining DOUBLE) RETURNS STRING
```

**What it computes:** a pure classification, no table access —

```
CRITICAL  -> stockout_risk = 'HIGH'  OR days_remaining <= 3
HIGH      -> days_remaining <= 7
MEDIUM    -> days_remaining <= 14
LOW       -> otherwise (or days_remaining IS NULL)
```

`stockout_risk` is Data Engineering's own precomputed
`fact_inventory_snapshot.STOCKOUT_RISK` (LOW/MEDIUM/HIGH) for the latest
snapshot row — it acts as an override so a DE-flagged HIGH-risk part is
always at least CRITICAL regardless of the forecast math.

**Use case:** turning a stockout date + risk signal into the one-word urgency
label used to order/prioritize a batch of candidates (CRITICAL first).

**Edge cases:** since this takes plain scalar inputs (not `part_id`/
`warehouse_id`), it must be composed with `predicted_stockout_date` (for
`days_remaining`, via `datediff(predicted_stockout_date(...), current_date())`)
and a snapshot's `STOCKOUT_RISK` — it never looks anything up itself.

**Example:**
```sql
SELECT ab_training.agentic_restock.classify_urgency(
  'HIGH',
  datediff(ab_training.agentic_restock.predicted_stockout_date('P1003', 'WH003'), current_date())
) AS urgency_level
```

---

## 4. `requested_restock_qty`

```sql
requested_restock_qty(part_id STRING, warehouse_id STRING) RETURNS INT
```

**What it computes:** `GREATEST(MAX_STOCK_LEVEL - QUANTITY_ON_HAND, 0)` from
the latest `fact_inventory_snapshot` row — the quote line-item quantity.
`MAX_STOCK_LEVEL` is the restock target; floored at 0 so it never suggests a
negative order.

**Use case:** "how many units should we order?" — the number that goes on
the quote, and one half of the veto comparison (see below).

**Edge cases:** returns `NULL` if the part/warehouse has no snapshot rows at
all (as opposed to `0`, which means "already at/above target").

**Example:**
```sql
SELECT ab_training.agentic_restock.requested_restock_qty('P1001', 'WH001') AS requested_restock_qty
```

---

## 5. `pending_procurement_qty`

```sql
pending_procurement_qty(part_id STRING, warehouse_id STRING) RETURNS DOUBLE
```

**What it computes:** `SUM(PENDING_QTY)` across open (`ISSUED`/`PARTIAL`)
purchase orders for this part, at the warehouse's linked plant
(`gold_dev.dim.dim_warehouse.LINKED_PLANT_ID`), from
`gold_dev.supply_chain_analytics.fact_procurement`.

**Use case:** the *veto input* — "is there already an in-flight purchase
order covering some or all of this need?" Genie compares this against
`requested_restock_qty` itself; see
[Composing the restock veto](#composing-the-restock-veto).

**Edge cases:** returns `0.0` — not `NULL` — both when there's genuinely no
open PO **and** when the warehouse has no linked plant at all (e.g. some
REGIONAL SPARES warehouses). Both cases mean "no confirmed coverage," so
they're deliberately not distinguished by this function; use
`open_procurement_orders` (function 6) or a direct `dim_warehouse` lookup if
you need to tell them apart.

**Example (live, from a real Lakeflow-flagged candidate):**
```sql
SELECT
  ab_training.agentic_restock.requested_restock_qty('P1003', 'WH003') AS requested_restock_qty,
  ab_training.agentic_restock.pending_procurement_qty('P1003', 'WH003') AS pending_procurement_qty
-- -> requested_restock_qty = 1690, pending_procurement_qty = 0.0
--    (no open PO covers it -> genuinely needs restocking)
```

---

## 6. `open_procurement_orders`

```sql
open_procurement_orders(part_id STRING, warehouse_id STRING)
RETURNS TABLE (purchase_order_id STRING, status STRING, pending_qty INT, expected_date DATE, supplier_name STRING)
```

**What it computes:** the row-level open (`ISSUED`/`PARTIAL`) purchase
orders behind `pending_procurement_qty` — same join, but returns each PO
(id, status, pending qty, expected delivery date, supplier), largest
`pending_qty` first, instead of just the sum.

**Use case:** the veto input to reach for when the answer needs to *name*
something — "which PO is covering this, from which supplier, and when is it
expected?" — rather than only reporting a number.

**Edge cases:** an empty result set is ambiguous the same way `0.0` is for
`pending_procurement_qty` — either no open PO exists, or the warehouse has no
linked plant. `supplier_name` can be `NULL` if the supplier dimension row
isn't resolvable (`LEFT JOIN`, filtered to `dim_supplier.IS_CURRENT = true`).

**Example (live):**
```sql
SELECT * FROM ab_training.agentic_restock.open_procurement_orders('P1004', 'WH007')
-- -> ('PO-2026-00104', 'ISSUED', 1000, '2026-08-19', 'Enkei Wheels India Ltd')
```

---

## 7. `restock_candidate_summary`

```sql
restock_candidate_summary(part_id STRING, warehouse_id STRING) RETURNS STRING
```

**What it computes:** a deterministic, templated natural-language paragraph
combining functions 1, 2, 3, 4, and the latest snapshot's on-hand/safety-stock
numbers — e.g. *"Front Ventilated Disc Brake Pad at WH003: 110 units on hand
(safety stock 200). Avg consumption 0.0/day. No forecastable stockout
(near-zero consumption). Urgency: CRITICAL. Suggested reorder: 1690 units."*
It's also the text source for the Teams Adaptive Card /
`quote_metadata.summary_report`.

**Use case — deliberately narrow:** use this **only** for the literal
phrasing "why does X need restocking" / "summarize this restock candidate."
It is **not** Genie's general-purpose explanation tool — see
[When to use the canned summary vs. compose your own](#when-to-use-the-canned-summary-vs-compose-your-own).

**Edge cases:** does **not** incorporate the veto (`pending_procurement_qty`)
at all — it only ever says a candidate needs restocking, never that it's
covered. If you need veto-aware text, compose it from functions 4–6 instead.

**Example:**
```sql
SELECT ab_training.agentic_restock.restock_candidate_summary('P1001', 'WH001') AS summary
```

---

## 8. `avg_lead_time_days`

```sql
avg_lead_time_days(part_id STRING) RETURNS DOUBLE
```

**What it computes:** the empirical average `EXPECTED_DATE_KEY -
ORDER_DATE_KEY` across all of a part's historical purchase orders (any
supplier/plant), from `fact_procurement`. **Not** a contracted SLA — there's
no fixed `lead_time_days` config field anywhere in `gold_dev` (checked
`dim_part`, `dim_supplier`, `fact_procurement`), so this is a derived
estimate.

**Use case:** informational context in an answer ("this part usually takes
~12 days to arrive once ordered") — purely descriptive text, never used by
`classify_urgency` or the veto comparison.

**Edge cases:** only takes `part_id` — no `warehouse_id`, since lead time is
a supplier/part property, not a warehouse one. Returns `NULL` if the part has
no procurement history at all.

**Example:**
```sql
SELECT ab_training.agentic_restock.avg_lead_time_days('P1001') AS avg_lead_time_days
```

---

## 9. `latest_snapshot`

```sql
latest_snapshot(part_id STRING, warehouse_id STRING)
RETURNS TABLE (snapshot_date DATE, quantity_on_hand INT, safety_stock_qty INT, max_stock_level INT, stockout_risk STRING)
```

**What it computes:** the single most recent
`gold_dev.supply_chain_analytics.fact_inventory_snapshot` row for a
part/warehouse, already deduped via `QUALIFY ROW_NUMBER() OVER (ORDER BY
SNAPSHOT_DATE_KEY DESC) = 1`.

**Use case:** the guardrail for plain "what's the current stock of X"
questions. `fact_inventory_snapshot` is a Genie Space data source Genie can
query directly, but it's a **daily snapshot fact** (one row per part ×
warehouse × day), not a single current-state row — an ad-hoc `SELECT`
without this dedup can silently double-count or pick a stale day. Calling
this function instead removes that failure mode.

**Edge cases:** an empty result means the part/warehouse combination has no
snapshot rows at all (e.g. it's never been stocked at that warehouse).

**Example (live, matches a real Lakeflow-flagged candidate):**
```sql
SELECT * FROM ab_training.agentic_restock.latest_snapshot('P1003', 'WH003')
-- -> ('2026-08-14', 110, 200, 1800, 'HIGH')
```

---

## Composing the restock veto

There is deliberately **no single `needs_restock(part_id, warehouse_id) →
BOOLEAN` function** (an earlier revision had one — see the design note in the
notebook's header cell for why it was removed). The veto is the one piece of
reasoning that *is* Genie's actual job (architecture §4.2 step 2), so it's
composed live from two atomic functions instead of hidden behind a boolean:

```
requested = requested_restock_qty(part_id, warehouse_id)
pending   = pending_procurement_qty(part_id, warehouse_id)

pending >= requested  -> likely false positive; an open PO already covers it
pending <  requested  -> genuinely needs restocking; gap = requested - pending
                         (pending = 0.0 covers both "no open PO" and
                         "no linked plant at all")
```

Call `open_procurement_orders(part_id, warehouse_id)` in addition when the
answer needs to name the specific PO/supplier/expected date backing that
coverage, rather than only reporting the two numbers.

**Verified live** (see `docs/agent_bricks_mapping.md` §2.2 for the full
Genie Space config): asking *"Does part P1003 at warehouse WH003 genuinely
need restocking, or could it be a false positive already covered by an open
purchase order?"* makes Genie call both functions and answer *"the requested
restock quantity is 1,690 units, while the pending procurement quantity is
0.0 units... restocking is genuinely required for this part and location."*

## When to use the canned summary vs. compose your own

`restock_candidate_summary` (function 7) is a fixed template — use it only
for the single literal phrasing "why does X need restocking" /
"summarize this restock candidate." For anything else — comparing two or
more candidates, ranking/prioritizing a batch, hypothetical/"what-if"
questions, or veto-aware explanations — compose the answer from the atomic
functions (1, 2, 3, 4, 5, 6, 8, 9) instead of forcing the question through
one fixed paragraph. This scoping is enforced in the Genie Space's
`text_instructions`, not in the function itself.

## Implementation notes (if you touch these functions)

- Internal calls between functions must be **fully qualified**
  (`ab_training.agentic_restock.avg_daily_consumption(...)`), not the bare
  function name, or `CREATE FUNCTION` fails to resolve them.
- **Scalar** functions (1–5, 7, 8) that do a table scan filtered down to one
  row must wrap the result in an aggregate (`MAX(...)`, `MAX_BY(...)`) —
  Spark's SQL function validator rejects correlated scalar subqueries it
  can't prove return exactly one row, even when the filter is on a composite
  primary key.
- **Table-valued** functions (6, 9; `RETURNS TABLE (...)`) are exempt from
  that rule since they're allowed to return multiple rows — they use plain
  `WHERE` / `QUALIFY ROW_NUMBER()` filtering instead. However, they can't be
  called via a `LATERAL` join against a driving query that reuses the same
  table alias (e.g. `dp`/`dw`) as the function body itself — Databricks SQL
  rejects that as an unsupported correlated subquery. Call them with literal
  arguments, or from application code with values already in hand.
