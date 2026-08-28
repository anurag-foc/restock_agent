# Agentic Manufacturing Inventory Intelligence System
### Proposed System — Requirements & Feature Design
#### Databricks AgentBricks · Multi-Agent Pipeline · Human-in-the-Loop

---

## System Purpose

This document describes the proposed agentic inventory intelligence system for manufacturing environments. The system continuously monitors inventory, forecasts shortages, understands manufacturing constraints (BOM dependencies, supplier variability, plant capacity, procurement rules), and surfaces financially-framed, actionable recommendations to a human approver — before acting.

The system is not a threshold alerter. It is a manufacturing planning assistant that reasons across multiple dimensions to answer the question a production manager actually cares about:

> *"What do I need to order, from whom, how much, and what happens if I don't — right now?"*

---

## Core Workflow (Always-On)

```
Lakeflow Job (hourly)
  └─ Coarse stock check: QUANTITY_ON_HAND <= SAFETY_STOCK_QTY
       └─ Candidates found → invoke Supervisor Agent
            ├─ Intelligence Layer (Nuances 1–11, see below)
            ├─ Quote generated → fact_restock_request + quote_metadata
            ├─ Teams Adaptive Card → PM notified
            └─ Databricks Review App → PM reviews & approves
                 └─ On Approval → Restock Agent → fulfillment written back
```

The intelligence layer is what this document specifies. Everything else (trigger, approval flow, data write-back) is the existing foundation.

---

## Data Foundation (Existing Schema — Key Fields)

### `dim_part` (relevant to intelligence layer)

| Column | Use in System |
|---|---|
| `PART_TYPE` | ASSEMBLY / SUB-ASSEMBLY / COMPONENT — drives BOM traversal |
| `BOM_LEVEL` | BOM hierarchy depth — identifies which parts are finished goods vs. raw components |
| `CRITICALITY_CLASS` | A-CRITICAL / B-MAJOR / C-MINOR — weights urgency prioritization |
| `ABC_CLASS` | ABC classification — informs financial framing |
| `SAFETY_CRITICAL` | Boolean — hard override: safety-critical parts always escalate to CRITICAL urgency |
| `UNIT_COST` | Standard unit cost (INR) — directly used for financial impact calculation, no new data needed |

### `dim_plant` (relevant to capacity nuance)

Currently holds plant metadata (`PLANT_TYPE`, `PLANT_STATUS`, `LOCATION`) but **no capacity columns**. A `plant_capacity` table owned by this repo is required (see Nuance 9).

### `fact_procurement` (relevant to supplier nuances)

Used for lead time estimation and open-PO veto. A `REJECTED_QTY` column can be added to support supplier defect/rejection rate scoring (Nuance 5) — no separate quality system needed.

---

## Intelligence Nuances

Organized into 4 tiers. Each nuance is independent and can be implemented incrementally.

---

## Tier 1 — Forecasting Intelligence

### Nuance 1 · Variable Supplier Lead Times → Dynamic Reorder Point

**Problem:**
The system currently uses a static `SAFETY_STOCK_QTY` column. It doesn't know which supplier will fulfil the order or how long they take. Supplier A may deliver in 7 days; fallback Supplier B takes 25. The reorder point must be computed dynamically per supplier scenario — not read from a fixed column.

**What the system surfaces:**
> "Part P1003 — 15 units/day consumption. Current stock: 140.
> Preferred supplier (7-day lead): reorder point = 105. ✅ Safe.
> Fallback supplier (25-day lead): reorder point = 375. ⚠️ Already 235 units short under fallback scenario.
> **Recommendation: Order now to cover worst-case lead time.**"

**New capability:**
- UC function: `dynamic_reorder_point(part_id, warehouse_id, supplier_id)` → `avg_daily_consumption × lead_time_days`
- Genie Agent instruction: surface both preferred + fallback scenarios when lead-time variance > 5 days
- New data needed: `lead_time_days` per `(part_id, supplier_id)` → `supplier_contract` table

---

### Nuance 2 · Demand Seasonality & Trend Detection

**Problem:**
The flat 14-day trailing average treats all days equally and misses seasonal spikes (quarterly production peaks, festival shutdowns) and upward consumption trends. The result: urgency is under-reported before demand surges.

**What the system surfaces:**
> "Trailing 14-day avg: 12 units/day.
> Same period last year: 2.1× spike in September.
> Adjusted forecast: **25 units/day** for next 30 days.
> Revised stockout date: **Sep 11** (not Sep 28).
> Urgency: MEDIUM → **HIGH**."

**New capability:**
- UC function: `seasonality_adjusted_consumption(part_id, warehouse_id, forecast_days)` — same-period prior-year multiplier
- `classify_urgency` receives `adjusted_days_remaining` as an additional input alongside existing `stockout_risk`
- MLflow evaluation tracks flat avg vs. adjusted forecast accuracy over time

---

### Nuance 3 · Consumption Anomaly Detection

**Problem:**
A sudden 5× consumption spike over 2 days silently inflates the average and triggers a quote. The spike might be a batch data entry error or a one-time project pull. Acting on bad data is expensive.

**What the system surfaces:**
> "⚠️ Anomaly: Part C-087 — 450 units issued in 2 days (baseline: ~18/day). Z-score: 12.5.
> Flagging before quote generation.
> **Supervisor Agent pauses and escalates to PM for signal confirmation.**"

**New capability:**
- UC function: `consumption_anomaly_score(part_id, warehouse_id)` → z-score of 2-day window vs. 90-day baseline
- Supervisor Agent routing: if score exceeds threshold → insert "verify consumption" step before quote generation
- MLflow: anomaly flags logged as labeled evaluation data (noise vs. real)

---

## Tier 2 — Procurement Intelligence

### Nuance 4 · MOQ, Pack Sizes & Procurement Constraints

**Problem:**
`requested_restock_qty` returns the mathematically ideal gap. Suppliers sell in fixed MOQs and pack sizes. Quoting 1,370 units when the MOQ is 2,000 produces an infeasible order — the PM approves a number procurement cannot place.

**What the system surfaces:**
> "Required: **1,370 units**
> Supplier MOQ: 2,000 · Pack size: 500 → Feasible order: **2,000 units**
> Excess: 630 units · Holding cost: ₹3,150/month (at `UNIT_COST` from `dim_part`)
> Excess absorbed in ~42 days at current consumption rate.
> **Quote is operationally feasible.**"

**New capability:**
- UC function: `feasible_order_qty(part_id, supplier_id, ideal_qty)` → `CEIL(ideal / pack_size) × pack_size`, clamped to `>= MOQ`
- Quote enrichment: `fact_restock_request` lines gain `ideal_qty`, `feasible_order_qty`, `excess_qty`, `excess_holding_cost_estimate`
- `UNIT_COST` from `dim_part` used directly — no new cost data needed
- New data needed: `moq`, `pack_size` per `(part_id, supplier_id)` → `supplier_contract` table

---

### Nuance 5 · Supplier Reliability Scoring

**Problem:**
Supplier selection is currently informational (lead time only). A supplier who delivers in 7 days but rejects 20% of shipments is worse than one who takes 10 days reliably. The agent must recommend the most reliable supplier, not just the fastest.

**What the system surfaces:**
> "3 eligible suppliers for Part P1003:
>
> | Supplier | Lead Time | On-Time % | Defect Rate | Score |
> |---|---|---|---|---|
> | Enkei Wheels | 7 days | 94% | 1.2% | ⭐ 91 — Recommended |
> | BorgWarner | 10 days | 88% | 0.8% | 79 |
> | Local Vendor | 5 days | 61% | 8.4% | 44 — Not recommended |"

**New capability:**
- `REJECTED_QTY` column added to `fact_procurement` (no separate quality system)
- UC function: `supplier_reliability_score(part_id, supplier_id)` → weighted composite (on-time %, defect rate) from `fact_procurement` history
- UC function: `ranked_suppliers(part_id, warehouse_id)` → table of eligible suppliers sorted by score
- Genie Agent instruction: always call `ranked_suppliers` before quoting a supplier

---

### Nuance 6 · Inter-Warehouse Lateral Transfers

**Problem:**
Before raising an external PO, the system should check whether another warehouse in the network has surplus stock of the same part. Internal transfers are faster, cheaper, and reduce unnecessary procurement spend.

**What the system surfaces:**
> "WH003 needs 1,370 units of Part P1003.
> Network check → WH007 (Chennai): 2,100 on hand · Safety stock: 500 · **Surplus: 1,600** ✅
> **Option A: Transfer 1,370 from WH007** — 3-day lead, ₹0 procurement cost.
> **Option B: External PO (Enkei Wheels)** — 7-day lead, ₹1,40,000.
> Agent recommendation: Option A."

**New capability:**
- UC function: `network_surplus(part_id, requesting_warehouse_id)` → warehouses with available surplus (on-hand minus safety stock)
- Supervisor Agent routing: if surplus found → Option A (transfer) presented before Option B (PO) in Teams card and Review App
- No new data needed — uses existing `fact_inventory_snapshot`

---

## Tier 3 — Manufacturing Intelligence

### Nuance 7 · BOM Dependency / Dependent Demand

**Problem:**
Parts are monitored independently. In manufacturing, finished-goods demand creates derived component demand. A production plan for Product A creates demand for its 4+ sub-components — but those components are only flagged reactively when their own threshold is breached.

**Note:** `dim_part` already has `PART_TYPE` (ASSEMBLY / SUB-ASSEMBLY / COMPONENT) and `BOM_LEVEL` — the hierarchy is partially embedded. A `bom` relationship table is still needed to link parent → child with `qty_per_unit`.

**What the system surfaces:**
> "Production plan: 500 units of Product A.
> BOM explosion:
> → Bearing B-220: need 2,000 · stock 850 · **Shortfall: 1,150** ⚠️
> → Motor M-110: need 1,000 · stock 1,340 · Surplus ✅
> → Seal Ring SR-05: need 500 · stock 620 · Surplus ✅
> **Bearing B-220 blocks assembly of 287 units. Restock triggered proactively.**"

**New capability:**
- New table: `ab_training.agentic_restock.bom` — `(fg_part_id, component_part_id, qty_per_unit)`
- UC function: `bom_component_requirements(fg_part_id, forecast_qty)` → `(component_part_id, required_qty, current_stock, gap_qty)`
- Genie Agent: new question type — "explode this finished-good forecast into component requirements and flag shortfalls"

---

### Nuance 8 · Component Shortages & Multi-Level Consequences

**Problem:**
Having 99% of components ready doesn't mean 99% of production proceeds. One ₹50 part can halt assembly of a ₹50,000 finished good. The system must find the constraining component and quantify the production value at risk.

**`UNIT_COST` and `CRITICALITY_CLASS` from `dim_part` are used directly here.**

**What the system surfaces:**
> "Product A — 11 components checked. 10 adequate. **1 critical gap:**
> Component C-087 (spring clip) — `CRITICALITY_CLASS: A-CRITICAL` · `UNIT_COST: ₹50`
> Stock: 820 · Consumption: 68/day · **Stockout in 12 days**
> **At risk: 2,400 units of Product A (₹50,000 each) = ₹12 crore production value**
> This is the constraining component. Prioritize immediately."

**New capability:**
- UC function: `assembly_risk_report(fg_part_id)` → explodes BOM, calls `predicted_stockout_date` per component, returns `(constraining_component, stockout_days, units_at_risk, production_value_at_risk)`
- `production_value_at_risk = units_at_risk × UNIT_COST` (from `dim_part`, no new data)
- Supervisor Agent: this result becomes the headline callout in Teams card and Review App
- `quote_metadata` gains an `assembly_risk` JSON field

---

### Nuance 9 · Production Capacity Constraints

**Problem:**
A 10,000-unit demand signal is wrong input for a procurement quote if the plant can only produce 7,500. `dim_plant` has plant metadata but no capacity fields — a `plant_capacity` table is required to close this gap.

**What the system surfaces:**
> "Demand forecast: 10,000 units of Product A.
> PLT-01 (linked to WH003) — available capacity this period: **7,500 units** ⚠️ Gap: 2,500
>
> Options:
> A. Overtime at PLT-01 → +1,500 units
> B. Route overflow to PLT-02 (WH007) → 4,000 units available ✅
> C. Deschedule Product B (lower ABC class) → frees 2,800 units
>
> **Recommendation: Option B. No overtime cost, no schedule disruption.**"

**New capability:**
- New table: `ab_training.agentic_restock.plant_capacity` — `(plant_id, period_start, period_end, available_capacity_units)`
- UC function: `plant_capacity_check(plant_id, required_qty, period)` → `(available, gap, overflow_plants)`
- Supervisor Agent: capacity gap → **capacity alert** surfaced alongside (or before) the standard restock quote

---

## Tier 4 — Financial Intelligence

### Nuance 10 · Cost of Stockout vs. Cost of Overstock

**Problem:**
The PM sees urgency labels and stock numbers — no financial context. Two CRITICAL alerts may have wildly different business impact. Financial framing turns gut-feel approvals into quantified decisions.

**`UNIT_COST` from `dim_part` and `ABC_CLASS` / `CRITICALITY_CLASS` are used directly — no new cost data needed.**

**What the system surfaces:**
> "**Decision frame — Quote QT-2026-0041:**
>
> If NOT approved:
> Stockout in 4 days · ~320 units/day production halt
> Revenue at risk: **₹16 lakh/day**
>
> If approved (2,000 units, MOQ):
> Procurement: ₹1,40,000 · Excess holding: ₹3,150/month
> Break-even on order: **Day 2 of production continuity**
>
> **Cost of inaction is 45× the cost of overstock. Order.**"

**New capability:**
- UC function: `stockout_cost_estimate(part_id, warehouse_id, stockout_days)` → `stockout_days × daily_production_rate × unit_margin`
- UC function: `overstock_cost_estimate(part_id, excess_qty, months)` → `excess_qty × UNIT_COST × holding_rate`
- Review App: new "Financial Impact" card rendered alongside the quote
- `daily_production_rate` sourced from `plant_capacity` table (Nuance 9 dependency)

---

### Nuance 11 · What-If Scenario Planning

**Problem:**
The PM approves or rejects a static quote. Real decisions are contextual — "What if demand goes up 20%?" or "What if the supplier is delayed 2 weeks?" There's no way to explore scenarios before committing.

**What the system surfaces (interactive in Review App):**
> **Scenario: Demand +20% in September**
> Current order covers 42 days at baseline → covers only **35 days** at +20%.
> Suggest 2,500 units (next MOQ step) to cover the upside.
>
> **Scenario: Preferred supplier delayed 2 weeks**
> Stockout moves: Sep 28 → **Sep 14**.
> Split order: 1,000 units BorgWarner (fast) + 1,000 units Enkei (reliable).

**New capability:**
- Genie Agent's natural-language interface handles this natively — no new UC functions required
- Review App gains a "Simulate Scenario" input panel; PM types a scenario, Genie query fires with modified parameters
- MLflow: scenario queries logged as PM feedback data — informs future default recommendation improvements

---

## Proposed Agent Architecture

### Current Structure
```
Supervisor Agent
  └─ Genie Agent (9 UC functions over gold_dev star schema)
  └─ Restock Agent
```

### Proposed: Tier-Based Subagents

The addition of 11+ new UC functions across 4 distinct reasoning domains creates a case for specialized subagents. A single Genie Space with 20+ functions becomes hard to instruct precisely. Splitting by tier gives each agent a focused, testable responsibility.

```
Supervisor Agent
  ├─ Forecasting Agent     (Nuances 1–3: lead time scenarios, seasonality, anomaly)
  ├─ Procurement Agent     (Nuances 4–6: MOQ, supplier reliability, lateral transfers)
  ├─ Manufacturing Agent   (Nuances 7–9: BOM explosion, assembly risk, capacity)
  ├─ Financial Agent       (Nuances 10–11: cost framing, what-if simulation)
  └─ Restock Agent         (fulfillment write-back, unchanged)
```

**Why subagents make sense here:**
- Each tier has a distinct UC function set and data domain — Genie Space instructions stay focused
- Tiers 1–3 and 4–6 can run **in parallel** for a given candidate — Supervisor Agent fans out, waits for all results before composing the quote
- MLflow evaluation is cleaner — each subagent is evaluated independently (e.g., "did the Forecasting Agent correctly flag the anomaly?")
- Aligns with AgentBricks' Supervisor Agent + specialist subagent pattern

**Trade-off to discuss:**
- Genie Spaces are the natural home for UC functions in Databricks. Multiple specialized Genie Spaces = multiple Genie Space resources to manage
- Alternative: keep one enriched Genie Space but have the Supervisor Agent send targeted, scope-limited natural-language questions to it per tier (simulates subagent reasoning without separate resources)
- Recommendation: start with one enriched Genie Space + scoped questions, split into separate Genie Spaces once the UC function count exceeds 15–20

---

## Full Nuance Summary

| # | Tier | Nuance | Key Output | New Data | New UC Function |
|---|---|---|---|---|---|
| 1 | Forecasting | Variable supplier lead times | Per-supplier reorder point | `lead_time_days` in `supplier_contract` | `dynamic_reorder_point` |
| 2 | Forecasting | Demand seasonality & trend | Adjusted stockout date | Prior-year history (existing) | `seasonality_adjusted_consumption` |
| 3 | Forecasting | Consumption anomaly detection | Anomaly flag + pause routing | — | `consumption_anomaly_score` |
| 4 | Procurement | MOQ / pack sizes | Feasible order qty + excess cost | `moq`, `pack_size` in `supplier_contract` | `feasible_order_qty` |
| 5 | Procurement | Supplier reliability scoring | Ranked supplier recommendation | `REJECTED_QTY` on `fact_procurement` | `supplier_reliability_score`, `ranked_suppliers` |
| 6 | Procurement | Inter-warehouse lateral transfers | Transfer-first before PO | — (existing snapshot) | `network_surplus` |
| 7 | Manufacturing | BOM dependent demand | Proactive component shortfall | `bom` table | `bom_component_requirements` |
| 8 | Manufacturing | Component shortages & consequences | Constraining part + ₹ at-risk | `UNIT_COST` on `dim_part` (existing) | `assembly_risk_report` |
| 9 | Manufacturing | Production capacity constraints | Capacity gap + overflow options | `plant_capacity` table | `plant_capacity_check` |
| 10 | Financial | Cost of stockout vs. overstock | Financial decision frame | `UNIT_COST` on `dim_part` (existing) | `stockout_cost_estimate`, `overstock_cost_estimate` |
| 11 | Financial | What-if scenario planning | Interactive PM scenario simulation | — | Genie natural-language (no new function) |

---

## New Tables (Owned by `ab_training.agentic_restock`)

| Table | Key Columns | Required By |
|---|---|---|
| `supplier_contract` | `part_id`, `supplier_id`, `lead_time_days`, `moq`, `pack_size` | Nuances 1, 4, 5 |
| `bom` | `fg_part_id`, `component_part_id`, `qty_per_unit` | Nuances 7, 8 |
| `plant_capacity` | `plant_id`, `period_start`, `period_end`, `available_capacity_units` | Nuances 9, 10 |

---

## Open Questions

| # | Question | Status |
|---|---|---|
| 1 | Does `gold_dev` have a BOM/parts-relationship table, or do we seed `bom` from a mock? | Ask Data Engineering |
| 2 | How many years of `fact_inventory_transaction` history exist? (Seasonality needs ≥1 prior year) | Ask Data Engineering |
| 3 | Does Databricks App framework support streaming Genie responses for the what-if panel? | Need to validate |
| 4 | `dim_plant` has no capacity columns confirmed — `plant_capacity` table will be owned by this repo | Decided |
| 5 | `UNIT_COST` on `dim_part` confirmed — no new cost data needed for financial nuances | Confirmed |
| 6 | `REJECTED_QTY` column to be added to `fact_procurement` for supplier defect scoring | Decided |
