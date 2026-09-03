# Unity Catalog Functions Reference — `gold_dev.supply_chain_analytics`

Complete reference for the **23** governed Unity Catalog SQL functions registered under `gold_dev.supply_chain_analytics`, in two generations:

| Generation | Count | Source | Reads |
|---|---|---|---|
| **Deep analysis** (Part 1 below) | 16 | `notebooks/uc_functions/deep_analysis_functions.ipynb` | DE's fact/dim tables directly |
| **Phase-1 priority** (Part 2 below) | 7 | `notebooks/uc_functions/priority_functions.py` over `src/agentic_restock/jobs/priority_functions.py` | `inventory_signal_board` only |

**Catalog & Schema:** `gold_dev.supply_chain_analytics`  
**Deployment Job:** `deploy_uc_functions` — three tasks, **in order**: `deploy_functions` → `refresh_signal_board` → `deploy_priority_functions`. Spark validates a function body at `CREATE` time, so the phase-1 functions cannot be created before the board exists.

All 23 are attached to the `genie_agent` Genie Space as trusted assets, and to nothing else — the Supervisor Agent never calls a UC function directly. See [`agent_bricks_mapping.md`](agent_bricks_mapping.md) §2.3/§2.4.

> **Which generation should new work use?** The phase-1 functions. They read the
> board, so there is one source of truth per part/warehouse. Several
> deep-analysis functions are now redundant with board columns and the three
> that return narrative `STRING`s were a workaround for having no LLM in the
> loop — Genie writes the sentence now. They are retained because
> `fulfillment_guardrail` and the fulfillment path were never audited for
> dependence on them; audit before deleting any.

---

# Part 1 — Deep-analysis functions (16)

Implementing the 11 intelligence nuances across 4 tiers from `prd_v2.md`.

---

## Intelligence Tiers & Nuances Overview

| Tier | Intelligence Nuance | Governed UC Function | Kind | Return Type | Description |
|---|---|---|---|---|---|
| **Tier 1: Forecasting** | **Nuance 1:** Dynamic Reorder Point | `dynamic_reorder_point` | Scalar | `INT` | Calculates reorder point anchored to contracted lead-time days |
| | **Nuance 2:** Seasonality & Trend | `seasonality_adjusted_consumption` | Scalar | `DOUBLE` | Adjusts consumption rate for seasonal multipliers |
| | | `predicted_stockout_date` | Scalar | `DATE` | Projects date of zero stock based on daily burn rate |
| | | `classify_urgency` | Pure | `STRING` | Classifies urgency: CRITICAL / HIGH / MEDIUM / LOW |
| | **Nuance 3:** Consumption Anomaly | `consumption_anomaly_score` | Scalar | `DOUBLE` | Z-score of recent 2-day usage vs 90-day baseline |
| **Tier 2: Procurement** | **Nuance 4:** MOQ & Pack Size | `feasible_order_qty` | Scalar | `INT` | Rounds ideal shortfall to MOQ & pack size multiples |
| | | `requested_restock_qty` | Scalar | `INT` | Ideal unconstrained shortfall (`MAX_STOCK - ON_HAND`) |
| | **Nuance 5:** Supplier Reliability | `supplier_reliability_score` | Scalar | `DOUBLE` | Composite score (0-100) combining defect % & OTD % |
| | | `ranked_suppliers` | Table | `TABLE` | Ranks suppliers by reliability, lead time & unit cost |
| | **Nuance 6:** Lateral Transfers | `network_surplus` | Table | `TABLE` | Scans network warehouses for available transfer stock |
| **Tier 3: Manufacturing** | **Nuance 7:** BOM Explosion | `bom_component_requirements` | Table | `TABLE` | Explodes finished good demand into sub-component gaps |
| | **Nuance 8:** Assembly Risk | `assembly_risk_report` | Scalar | `STRING` | Highlights constraining component & production value at risk |
| | **Nuance 9:** Plant Capacity | `plant_capacity_check` | Scalar | `STRING` | Checks required production against rated plant capacity |
| **Tier 4: Financial** | **Nuance 10:** Cost Tradeoff | `financial_tradeoff_summary` | Scalar | `STRING` | Financial analysis of stockout loss vs MOQ holding cost |
| | **Nuance 11:** What-If Simulation | *Genie / Supervisor Prompt* | Agent | Text/JSON | Dynamic parameter scenario modeling |

---

## Tier 1 — Forecasting Intelligence

### Nuance 1 · Dynamic Supplier Reorder Point

#### `dynamic_reorder_point`
```sql
gold_dev.supply_chain_analytics.dynamic_reorder_point(
  part_id STRING,
  warehouse_id STRING,
  supplier_id STRING
) RETURNS INT
```
- **Description:** Computes the dynamic reorder threshold `CEIL(avg_daily_consumption × contracted lead_time_days)` using contract terms from `dim_supplier_contract`. Replaces static safety stock columns with supplier-aware reorder triggers.
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.dynamic_reorder_point('PART-001', 'WH001', 'SUPP-001');
  ```

---

### Nuance 2 · Demand Seasonality & Trend Detection

#### `seasonality_adjusted_consumption`
```sql
gold_dev.supply_chain_analytics.seasonality_adjusted_consumption(
  part_id STRING,
  warehouse_id STRING,
  forecast_days INT DEFAULT 30
) RETURNS DOUBLE
```
- **Description:** Adjusts baseline daily consumption for seasonal trends by applying historical prior-year same-period demand multipliers.
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.seasonality_adjusted_consumption('PART-001', 'WH001', 30);
  ```

#### `predicted_stockout_date`
```sql
gold_dev.supply_chain_analytics.predicted_stockout_date(
  part_id STRING,
  warehouse_id STRING
) RETURNS DATE
```
- **Description:** Projects the estimated stockout date based on current `QUANTITY_ON_HAND` from `fact_inventory_snapshot` divided by 14-day `avg_daily_consumption`.
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.predicted_stockout_date('PART-001', 'WH001');
  ```

#### `classify_urgency`
```sql
gold_dev.supply_chain_analytics.classify_urgency(
  stockout_risk STRING,
  days_remaining DOUBLE
) RETURNS STRING
```
- **Description:** Pure classification function mapping risk signals to urgency levels:
  - `CRITICAL`: `stockout_risk = 'HIGH'` OR `days_remaining <= 3`
  - `HIGH`: `days_remaining <= 7`
  - `MEDIUM`: `days_remaining <= 14`
  - `LOW`: Otherwise

---

### Nuance 3 · Consumption Anomaly Detection

#### `consumption_anomaly_score`
```sql
gold_dev.supply_chain_analytics.consumption_anomaly_score(
  part_id STRING,
  warehouse_id STRING
) RETURNS DOUBLE
```
- **Description:** Calculates the statistical Z-score comparing recent 2-day daily issue rate against the 90-day baseline mean and standard deviation. Flagged spikes (> 3.0) signal potential data entry errors or unusual batch pulls.
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.consumption_anomaly_score('PART-001', 'WH001');
  ```

---

## Tier 2 — Procurement Intelligence

### Nuance 4 · MOQ, Pack Sizes & Procurement Constraints

#### `feasible_order_qty`
```sql
gold_dev.supply_chain_analytics.feasible_order_qty(
  part_id STRING,
  supplier_id STRING,
  ideal_qty INT
) RETURNS INT
```
- **Description:** Rounds the mathematically ideal restock shortfall up to the supplier's contracted Minimum Order Quantity (MOQ) and Pack Size increments from `dim_supplier_contract`.
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.feasible_order_qty('PART-001', 'SUPP-001', 350);
  -- Returns: 500 (if MOQ=500, Pack Size=100)
  ```

#### `requested_restock_qty`
```sql
gold_dev.supply_chain_analytics.requested_restock_qty(
  part_id STRING,
  warehouse_id STRING
) RETURNS INT
```
- **Description:** Computes raw shortfall `GREATEST(MAX_STOCK_LEVEL - QUANTITY_ON_HAND, 0)` from the latest inventory snapshot.

---

### Nuance 5 · Supplier Quality & Reliability Scoring

#### `supplier_reliability_score`
```sql
gold_dev.supply_chain_analytics.supplier_reliability_score(
  supplier_id STRING
) RETURNS DOUBLE
```
- **Description:** Computes a composite reliability score (0-100) combining defect quality PPM (`fact_supplier_quality`) and On-Time Delivery percentage (`fact_supplier_delivery`).
- **Example:**
  ```sql
  SELECT gold_dev.supply_chain_analytics.supplier_reliability_score('SUPP-001');
  ```

#### `ranked_suppliers`
```sql
gold_dev.supply_chain_analytics.ranked_suppliers(
  part_id STRING
) RETURNS TABLE (
  supplier_id STRING,
  lead_time_days INT,
  moq INT,
  unit_cost DOUBLE,
  reliability_score DOUBLE,
  is_preferred BOOLEAN
)
```
- **Description:** Table-valued function returning all contracted suppliers for a given part, ordered by preference, reliability score, and lead time.

---

### Nuance 6 · Inter-Warehouse Lateral Transfers

#### `network_surplus`
```sql
gold_dev.supply_chain_analytics.network_surplus(
  part_id STRING,
  requesting_warehouse_id STRING
) RETURNS TABLE (
  warehouse_id STRING,
  warehouse_code STRING,
  on_hand INT,
  safety_stock INT,
  available_surplus INT
)
```
- **Description:** Scans all other warehouses in the network for available surplus stock (`QUANTITY_ON_HAND - SAFETY_STOCK_QTY > 0`), recommending internal transfers before raising external POs.

---

## Tier 3 — Manufacturing Intelligence

### Nuance 7 · BOM Component Explosions

#### `bom_component_requirements`
```sql
gold_dev.supply_chain_analytics.bom_component_requirements(
  fg_part_id STRING,
  target_fg_qty INT
) RETURNS TABLE (
  component_part_id STRING,
  qty_per_unit INT,
  total_component_needed INT,
  component_on_hand INT,
  shortfall_qty INT
)
```
- **Description:** Explodes a finished-good production forecast into child component requirements using `dim_bom`, comparing required component quantities against live inventory stock.

---

### Nuance 8 · Component Shortages & Production Value-at-Risk

#### `assembly_risk_report`
```sql
gold_dev.supply_chain_analytics.assembly_risk_report(
  fg_part_id STRING
) RETURNS STRING
```
- **Description:** Analyzes child component stock for a finished good assembly, identifies the single constraining bottleneck component, and quantifies potential finished good production value at risk.

---

### Nuance 9 · Production Capacity Constraints

#### `plant_capacity_check`
```sql
gold_dev.supply_chain_analytics.plant_capacity_check(
  plant_id STRING,
  required_qty INT
) RETURNS STRING
```
- **Description:** Compares target manufacturing volume against rated plant capacity from `fact_plant_capacity`, alerting if overtime or plant re-routing is required.

---

## Tier 4 — Financial Intelligence & Scenario Simulation

### Nuance 10 · Financial Risk: Stockout Cost vs. Overstock Holding Cost

#### `financial_tradeoff_summary`
```sql
gold_dev.supply_chain_analytics.financial_tradeoff_summary(
  part_id STRING,
  ideal_qty INT,
  moq INT
) RETURNS STRING
```
- **Description:** Generates a financial comparison comparing potential production stockout loss against excess inventory holding cost (15% annual carrying cost on MOQ excess).

---

### Nuance 11 · "What-If" Scenario Simulation
- **Mechanism:** Handled via Genie Agent and Supervisor Agent prompt routing. Users can query scenarios such as:
  - *"What if lead time for Supplier B increases by 10 days?"*
  - *"What if production target for Product A increases by 20%?"*

---

## Restock Veto & Operational Support Functions

- **`pending_procurement_qty(part_id, warehouse_id)`**: Sums `PENDING_QTY` across open (`ISSUED`/`PARTIAL`) POs at the warehouse's linked plant, to veto false-positive restock triggers.
- **`avg_daily_consumption(part_id, warehouse_id, lookback_days)`**: Trailing-window average consumption from `fact_inventory_transaction` `ISSUE` rows.

**Retired — do not reference these; they no longer exist:** `open_procurement_orders`, `latest_snapshot`, `restock_candidate_summary`, `avg_lead_time_days`, `needs_restock`. The PO-citation and current-stock lookups are served by the board and by Genie's direct table access; `restock_candidate_summary`'s canned paragraph was replaced by the Supervisor writing the summary itself. On why there is deliberately no `needs_restock` boolean, see [`agent_bricks_mapping.md`](agent_bricks_mapping.md) §2.3.

---

# Part 2 — Phase-1 priority functions (7)

Thin reads over `gold_dev.supply_chain_analytics.inventory_signal_board`, which
carries every nuance below as a **column** computed set-wise. Nothing here
recomputes what the board already knows — if the board says a part has network
surplus, `scan_transfer_options` cannot disagree with it. Nuance numbering is
[`market_evidence_phase1.md`](market_evidence_phase1.md) §7's, independent of
Part 1's.

Every `*_filter` parameter is optional and `NULL`-means-all, so each function
doubles as a whole-network scan and a single-part drill-down.

---

### Nuance 3 · Decision-Value Ranking — **the one that matters**

#### `rank_priority_actions`
```sql
gold_dev.supply_chain_analytics.rank_priority_actions(
  max_rows INT DEFAULT 5
) RETURNS TABLE (
  part_id STRING, warehouse_id STRING, signal_type STRING,
  exposure DOUBLE, action_cost DOUBLE, decision_value DOUBLE,
  best_donor_warehouse_id STRING, network_surplus_qty INT,
  threatened_parent_part_id STRING, preferred_supplier_id STRING,
  effective_lead_days DOUBLE, reliability_score DOUBLE,
  commitment_state STRING, commitment_age_days INT
)
```
- **Description:** Ranks by *what changes if a human acts* — exposure minus the cost of the cheapest viable fix — not by raw exposure. This is the function `invoke_supervisor.py` counts and Turn 1 calls; the whole pipeline's "which single thing matters most today" comes from here.
- **The formula, kept deliberately simple and visible so it can be argued with:**
  ```
  exposure       = value_at_risk (BOM cascade)
                   OR (safety_stock - on_hand) * unit_cost (direct shortage)
  action_cost    = exposure * 0.03                              if a transfer covers the shortfall
                 = exposure * (0.15 + min(lead_days,90)/90 * 0.35)   if only a buy exists
                 = exposure * 1.00                              if no fix exists at all
  decision_value = GREATEST(exposure - action_cost, 0)
  ```
- **`signal_type`** is derived, not stored: `BOM_CASCADE_RISK` if a threatened parent exists, else `STOCK_THRESHOLD` if below safety stock, and overridden to `STALLED_COMMITMENT` for a stale open commitment.
- **Suppression (nuance 8) is applied here, in SQL — not left to Genie.** An open commitment on the same part/warehouse means a human is already acting, so the row is suppressed. An LLM asked to remember this gets it right ~97% of the time, which means re-raising a rejected item about monthly.
- **Stalled commitments re-surface**, rather than staying invisible while exposure accrues: `PENDING_APPROVAL`/`NEEDS_REVIEW` after **2 days** (a defensible PM turnaround), `APPROVED`/`FULFILLING` after **`effective_lead_days + 3`** (execution plus a grace buffer). `REJECTED` matches neither branch and stays permanently suppressed — a closed decision.
- **Known simplification:** re-surfacing is time-based only. "Exposure grew 1.5× since the decision" would need an `EXPOSURE_AT_DECISION` captured at decision time, which `fact_restock_request` does not store.
- **Open validation item** ([`market_evidence_phase1.md`](market_evidence_phase1.md) §16): whether decision-value ranking actually reorders anything versus raw exposure on real data, and whether the cost weights above are right. They are not tuned against outcomes yet — treat them as provisional.
- **Implementation notes (two Databricks limits, both hit here):** a SQL UDF's `LIMIT` must be a literal, not a parameter (`INVALID_LIMIT_LIKE_EXPRESSION.IS_UNFOLDABLE`), and `QUALIFY` cannot see a UDF parameter once nested past two CTEs (`UNRESOLVED_COLUMN`). Hence the cap is a plain `WHERE rn <= max_rows` over a pre-ranked `ROW_NUMBER()` column.

---

### Nuance 1 · Inter-Warehouse Transfers

#### `scan_transfer_options`
```sql
gold_dev.supply_chain_analytics.scan_transfer_options(
  min_value DOUBLE DEFAULT 0,
  part_id_filter STRING DEFAULT NULL
) RETURNS TABLE (
  part_id STRING, warehouse_id STRING, on_hand INT, safety_stock INT,
  shortfall_qty INT, best_donor_warehouse_id STRING, network_surplus_qty INT,
  donor_cover_after_units INT, transfer_value DOUBLE
)
```
- **Description:** Parts short at one warehouse with real, **donor-protected** surplus at another (the donor's own cover after the transfer is reported, so a fix doesn't create a second shortage).
- **`transfer_value` is what buying the shortfall would have cost** — the money a transfer avoids spending, not merely stock relocated. That framing is why a transfer's `action_cost` is ~3% of exposure in the ranking above.
- Phase-1 successor to Part 1's `network_surplus`, differing in that the donor-protection and valuation are already on the board.

---

### Nuance 2 · BOM Cascade Value at Risk

#### `scan_assembly_risk`
```sql
gold_dev.supply_chain_analytics.scan_assembly_risk(
  min_value DOUBLE DEFAULT 0,
  part_id_filter STRING DEFAULT NULL
) RETURNS TABLE (
  component_part_id STRING, warehouse_id STRING,
  threatened_parent_part_id STRING, parent_units_blocked INT, value_at_risk DOUBLE
)
```
- **Description:** Components that look healthy on their own but still block an A-CRITICAL parent build target. `value_at_risk = parent_units_blocked * parent unit cost`.
- **Known approximation:** the board derives the parent's build target as `MAX_STOCK_LEVEL - QUANTITY_ON_HAND`, because there is no `FORECAST_QTY` or production-plan table. Revisit if Data Engineering exposes one ([`market_evidence_phase1.md`](market_evidence_phase1.md) §16).
- Returns a narrative-free row set; Part 1's `assembly_risk_report` returned a `STRING` and is superseded by this plus Genie writing the prose.

---

### Nuance 4 · Seasonality-Adjusted Burn

#### `scan_demand_shift`
```sql
gold_dev.supply_chain_analytics.scan_demand_shift(
  part_id_filter STRING DEFAULT NULL
) RETURNS TABLE (
  part_id STRING, warehouse_id STRING,
  flat_daily_burn DOUBLE, seasonal_multiplier DOUBLE,
  adj_daily_burn DOUBLE, days_of_cover DOUBLE
)
```
- **Description:** Parts whose seasonally-adjusted burn materially disagrees with the flat trailing average — multiplier outside **0.8–1.2×**, ordered by `ABS(seasonal_multiplier - 1.0)`.
- **Why it's worth its own function:** `adj_daily_burn` feeds `days_of_cover`, which feeds every other ranking in this module. If the seasonal correction is wrong, everything downstream inherits the error, so it needs to be inspectable on its own.
- Parts with under **90 days** of transaction history (`MIN_HISTORY_DAYS_FOR_SEASONALITY`) fall back to the flat snapshot average rather than a noisy multiplier.

---

### Nuance 5 · Lead-Time Reality Check

#### `scan_leadtime_drift`
```sql
gold_dev.supply_chain_analytics.scan_leadtime_drift(
  min_days DOUBLE DEFAULT 3,
  part_id_filter STRING DEFAULT NULL
) RETURNS TABLE (
  part_id STRING, preferred_supplier_id STRING,
  contracted_lead_days INT, observed_avg_delay_days DOUBLE,
  effective_lead_days DOUBLE, otd_rate DOUBLE, reliability_score DOUBLE
)
```
- **Description:** Parts whose contracted lead time and observed delivery reality have drifted apart by at least `min_days` — the stale-master-data signal.
- **One row per part, not per part/warehouse** (`SELECT DISTINCT`): lead time is a supplier property, not a warehouse one.
- `effective_lead_days = contracted + observed_avg_delay` is what the ranking's buy-side `action_cost` scales on, so drift here directly changes priority ordering.

---

### Nuance 6 · Supplier Reliability

#### `evaluate_suppliers`
```sql
gold_dev.supply_chain_analytics.evaluate_suppliers(
  part_id_filter STRING          -- required
) RETURNS TABLE (
  supplier_id STRING, lead_time_days INT, moq INT, pack_size INT,
  unit_cost DOUBLE, is_preferred BOOLEAN, otd_rate DOUBLE, reliability_score DOUBLE
)
```
- **Description:** **Every** contracted supplier for a part with observed reliability from `fact_supplier_delivery` — not just the preferred one the board carries — so a supplier switch can be justified against the incumbent. `is_preferred` marks the incumbent.
- The only phase-1 function whose part filter is **required**: it is a per-part comparison, not a network scan.

---

### Nuance 7 · MOQ / Pack Feasibility

#### `evaluate_feasibility`
```sql
gold_dev.supply_chain_analytics.evaluate_feasibility(
  part_id_filter STRING,
  supplier_id_filter STRING,
  ideal_qty INT
) RETURNS TABLE (
  feasible_qty INT, moq INT, pack_size INT,
  excess_qty INT, excess_holding_cost DOUBLE
)
```
- **Description:** Rounds an ideal shortfall up to what the supplier will actually accept — `GREATEST(moq, CEIL(ideal_qty / pack_size) * pack_size)` — and prices the excess that creates, so a recommendation is never one the customer cannot place.
- **Why this is a function and not a board column:** it needs a *chosen* quantity and supplier, which only exist once the agent has decided on a resolution. Every other nuance is knowable set-wise in advance; this one is not.
