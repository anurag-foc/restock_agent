# Unity Catalog Functions Reference — `gold_dev.supply_chain_analytics`

This document provides the complete technical and functional reference for all governed Unity Catalog SQL functions registered under `gold_dev.supply_chain_analytics`. These functions implement the 11 core intelligence nuances across 4 manufacturing intelligence tiers for the Restockify workflow.

**Catalog & Schema:** `gold_dev.supply_chain_analytics`  
**Deployment Notebook:** `notebooks/uc_functions/deep_analysis_functions.ipynb`  
**Deployment Job:** `deploy_uc_functions`

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

- **`pending_procurement_qty(part_id, warehouse_id)`**: Sums `PENDING_QTY` across open POs to veto false-positive restock triggers.
- **`open_procurement_orders(part_id, warehouse_id)`**: Table function listing specific open purchase orders.
- **`latest_snapshot(part_id, warehouse_id)`**: Table function returning deduped current stock snapshot.
- **`restock_candidate_summary(part_id, warehouse_id)`**: Canned natural-language summary paragraph.
