# Schema changes in `gold_dev_analytics` — record for Data Engineering

**Audience:** the Data Engineering team who own the `gold_dev` star schema.
**Status:** the two replica schema changes were APPLIED 2026-09-04. Each item below carries
its own status. **`gold_dev` itself has not been touched and will not be** — §1 items are
requests for DE to consider, not changes we have made.
**Context:** `gold_dev_analytics` is a replica catalog the Inventory Intelligence project
uses to generate a controlled dataset for the phase-1 detector redesign. Data generation
inside the replica is unrestricted; **schema shape changes are recorded here** so nothing
diverges from `gold_dev` silently.

Three categories below, and the distinction matters to DE:

- **§1 Requests to consider for production** — gaps we hit in DE's real schema that block a
  detector. These are not replica conveniences; the same limitation exists in `gold_dev`.
- **§2 Replica-only additions** — shape changes we need for the demo dataset that DE need not
  adopt.
- **§3 Data-quality observations** — no schema change, but the replica's current contents
  suggest upstream generator defects DE may want to know about.

---

## §1 Requests to consider for production

### 1.1 `fact_procurement` has no actual-receipt date — REQUEST

**Finding.** Columns are `ORDER_DATE_KEY`, `EXPECTED_DATE_KEY`, `RECEIVED_QTY`,
`PENDING_QTY`, `STATUS`. Nothing records **when goods actually landed**.

**Why it matters.** Observed-vs-contracted lead time is the cheapest, highest-value
master-data signal available (it explains why a customer's safety stock is mis-calibrated).
It requires `receipt_date − order_date`. With `EXPECTED_DATE_KEY` only, we can measure
*promise* but never *performance* — an expected date is not evidence of anything.

`docs/market_evidence_phase1.md` §4 asserts "we already hold fact_procurement, we can compute
OBSERVED lead time (PO date to receipt date)". **That claim is not true against this schema**
and has been corrected.

**Request.** Add `ACTUAL_RECEIPT_DATE_KEY INT` (and ideally `FIRST_RECEIPT_DATE_KEY` for
partial deliveries) to `fact_procurement` in `gold_dev`. If receipts are tracked in an
upstream source not yet landed in gold, knowing that is equally useful.

**Our workaround meanwhile.** Derive lead-time performance from `fact_supplier_delivery`
(`PLANNED_DATE_KEY` vs `DELIVERY_DATE_KEY`, `DELAY_DAYS`) instead. This is strictly weaker —
see 1.2.

### 1.2 `fact_supplier_delivery` has no `PART_KEY` — REQUEST + replica change

**Finding.** Grain is (supplier, warehouse, delivery). There is no part reference.

**Why it matters.** Lead time and reliability are properties of a **(supplier, part)** pair,
not a supplier alone. One supplier can be reliable on a commodity fastener and chronically
late on a machined casting; averaging them produces a number that describes neither. Without
`PART_KEY` the detector's most specific tier is unreachable and every part sourced from a
supplier inherits one blended figure.

**Request.** Add `PART_KEY BIGINT` to `fact_supplier_delivery` in `gold_dev`.

**Replica change — APPLIED 2026-09-04.** Existing 1100 rows carry `NULL`; the column is
populated by the dataset generator.
```sql
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_supplier_delivery
  ADD COLUMNS (PART_KEY BIGINT COMMENT 'FK to dim_part.PART_KEY. Added by the Inventory Intelligence project so supplier lead-time and reliability can be measured per supplier+part rather than per supplier alone. Not present in gold_dev as of 2026-09-04 -- see docs/schema_changes_gold_dev_analytics.md');
```

### 1.3 `fact_procurement` cannot attribute an open PO to a receiving warehouse — APPLIED 2026-09-05

```sql
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_procurement
  ADD COLUMNS (WAREHOUSE_KEY BIGINT);
-- backfilled from PLANT_KEY via dim_warehouse.LINKED_PLANT_ID: 1119/1119 rows, 0 unresolved
```

**A correction to the correction — 2026-09-16.** The paragraph below claimed the backfill was
lossless. It was not, and the claim was checkable at the time: after it ran, all 1,119 rows sat on
WH001 or WH002 and **not one open order was destined for a regional DC**. The cause is in
`generation/procurement.py`, which assigned `default_plant` to any warehouse with no plant link —
i.e. every DC — so the plant-link backfill resolved 528 DC-bound orders onto WH001. Every row
resolving is not the same thing as every row resolving correctly, and "1119/1119, 0 unresolved"
measured the first while reading as the second.

The generator now emits `WAREHOUSE_KEY` directly from the (part, warehouse) pair the order was
drawn for, so the column is populated at load time rather than by a follow-up UPDATE. That matters
beyond correctness: the backfill was a one-off statement outside the loader, so the next
`--prune --load` would have silently reset the column to NULL for every row. Reloaded on
2026-09-16; open orders now reach all eight warehouses (WH001 288, WH002 303, WH003-8 between 66
and 111 each).

**An honest correction to the argument below.** In this dataset the destination was never
actually *lost* — every procurement row carries a `PLANT_KEY`, and `LINKED_PLANT_ID` maps
PLT001→WH001 and PLT002→WH002 one-to-one, so the backfill was lossless **for plant stores**. The
measured 27%-vs-5% false-positive gap was therefore caused by the *detector not using* that link
for regional DCs, not by information being absent. The column still earns its place: it is exact
rather than inferred, and the plant-link trick breaks the moment a PO is raised against a
regional DC, which has no plant at all. `build_current_position_query` now prefers
`WAREHOUSE_KEY` and falls back to the plant link, so the same code runs against `gold_dev`, where
the column does not exist (`has_warehouse_key=False`).

#### Original finding, kept for the record

**Finding.** It carries `PLANT_KEY` but no `WAREHOUSE_KEY`. An open purchase order can therefore
be tied to a warehouse only via `dim_warehouse.LINKED_PLANT_ID`, which only plant stores have.

**Why it matters.** Availability is on-hand **plus on-order**, and that is what decides whether a
low balance is a shortage or merely a wait. Regional DCs have no plant link, so their inbound
reads as zero and their availability is understated — a stockout detector will flag a location
that in fact has stock arriving. Summing a part's inbound across all its warehouses instead
would be worse: it counts the same order once per location and makes every one of them look
covered.

**Request.** Add `WAREHOUSE_KEY BIGINT` to `fact_procurement`, or expose the receiving location
on whatever upstream source carries it.

**Measured cost of the gap.** Phase 4 quantified it on the loaded dataset by running the
scanners three times with different inbound attribution:

| inbound credited | findings raised | false shortages on quiet pairs |
|---|---|---|
| none | 286 | 116 / 237 (49%) |
| **plant stores only — what the schema allows today** | **183** | **63 / 237 (27%)** |
| complete — what `WAREHOUSE_KEY` would allow | **76** | **12 / 237 (5%)** |

So the gap costs roughly **5x the false-positive rate and 2.4x the finding volume**. A pair whose
stock is already on order is reported as a shortage, which is precisely the alert-fatigue failure
the product exists to avoid — and the alternative reading (sum a part's inbound across all its
warehouses) would count one order several times and make every location look covered.

**Current behaviour.** `jobs/positions.py` joins inbound through the plant link and accepts zero
for regional DCs, documented at the query. **Recommend applying `WAREHOUSE_KEY` to the replica**
on the strength of the numbers above — it is the highest-value outstanding schema change — but
it is a further divergence from `gold_dev`, so it needs a decision.

### 1.4 `fact_restock_request` extended to cover all eight action types — APPLIED 2026-09-05

**Gap.** The table was purchase-shaped: part, warehouse, supplier, quantity. Six of the eight
actions the redesign produces do not fit that — a transfer has a donor warehouse and no supplier,
a lead-time signal has no part at all, and a renegotiation has no quantity. Three quarters of what
the system finds could be *described* to a PM but not approved or rejected.

**Applied (replica only).** Six additive, nullable columns:

```sql
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_restock_request ADD COLUMNS (
  ACTION_TYPE STRING,              -- PURCHASE | TRANSFER | RENEGOTIATE | RESOURCE | RECALIBRATE | REVIEW_STOCK
  FINDING_TYPE STRING,             -- which detector raised it
  SUBJECT_KEY STRING,              -- the finding subject at its own grain
  SOURCE_WAREHOUSE_KEY BIGINT,     -- donor, for a TRANSFER
  RECOMMENDED_SUPPLIER_KEY BIGINT, -- for a RESOURCE action; SUPPLIER_KEY holds the incumbent
  EXPOSURE_AT_DECISION DECIMAL(18,2)
);
```

**`ACTION_TYPE` is load-bearing, not decorative.** The row grain is no longer purchases only, so
**summing `REQUESTED_QTY` across action types mixes units bought with units moved with units
merely flagged for review.** Any consumer of this table must filter on it. This column is the
reason the overload is safe: it makes the second meaning explicit rather than leaving a reader to
infer it.

`SUBJECT_KEY` exists because suppression cannot match a supplier-grain decision back to its
finding through `PART_KEY` — there is no part.

`EXPOSURE_AT_DECISION` was included while the table was open: CLAUDE.md records it as the missing
value that keeps suppression re-surfacing time-based only ("exposure grew 1.5x since the
decision" was unimplementable without it), and it is what the decision ledger needs to prove
realised ROI.

**Recorded dissent.** A cleaner shape would have been a separate decisions table we own, leaving
`fact_restock_request` meaning what its name says. That was raised and the single-table route was
chosen deliberately, with the overload made explicit via `ACTION_TYPE` as the mitigation. Noted
here so DE can see the tradeoff rather than discover it.

**Not requested for `gold_dev`** — this is a consequence of our own detector set, not a gap in
DE's schema. If the redesign ships, it becomes a request.

---

### 1.5 `fact_restock_request.PART_KEY` / `WAREHOUSE_KEY` made nullable — APPLIED 2026-09-05

```sql
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_restock_request
  ALTER COLUMN PART_KEY DROP NOT NULL;
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_restock_request
  ALTER COLUMN WAREHOUSE_KEY DROP NOT NULL;
```

**Why.** §1.4 extended this table to carry all eight action types, but two `NOT NULL` columns
made one of them unwritable. Three of the eight finding types are about a *supplier* rather than
a part, and `LEADTIME_SIGNAL` — "SUP010 is on contract on average but ±12 days, affecting 4
parts" — is filed at no part and no shelf. There is no honest `PART_KEY` for it.

**How it surfaced, which is the part worth reading.** `persist_quote` rejects a whole call if any
business key fails to resolve — deliberately, because a partially-written quote used to be
unrepairable. On the first complete run the model hit that rejection, **dropped the offending
candidate, and retried**, producing a 3-line quote for a 4-candidate run. It then exhausted the
approval-round budget without notifying anyone. The tool now distinguishes an **absent** key
(legitimate; a supplier-grain finding) from a **wrong** one (still refused for the whole call —
that check exists because a model once passed a part *name* where a `PART_ID` belonged and wrote
a header with zero lines).

**Alternatives rejected.** Filing the line against a "representative" affected part keeps the
column non-null but shows a PM a part that is not the subject of the decision they are approving.
Making the finding report-only leaves a PM able to read "your supplier is unpredictable" with no
way to act on it and no record they saw it — which undoes the reason §1.4 extended the table.

**For `gold_dev`: requested.** Unlike §1.4 this is not merely a consequence of our detector set —
any supplier-level or network-level action has the same problem, and a fact table whose grain is
"one part-line" cannot express one. If DE would rather keep the constraint, the alternative is a
sibling table at supplier grain, which was the original recommendation before §1.4.

---

### 1.6 `fact_restock_request.DECISION_REASON` — APPLIED 2026-09-05

```sql
ALTER TABLE gold_dev_analytics.supply_chain_analytics.fact_restock_request
  ADD COLUMNS (DECISION_REASON STRING COMMENT
    'Structured reason the PM chose alongside NOTE: NOT_A_PROBLEM | ALREADY_HANDLED | CANNOT_ACT_NOW | NUMBERS_WRONG | OTHER');
```

**Why a code and not just the note.** `NOTE` has existed all along and was never read back. The
problem is not storage, it is that free text cannot carry the one distinction that must change
behaviour: **"this is not a real problem" and "I cannot act on this right now" are opposite
signals that read almost identically.** Real notes are "no", "not now", "ask Rajesh". Treating a
deferral as a refusal means the system buries a live problem because someone was busy in the week
it was raised — alert fatigue inverted, and far harder to notice than the original, because
nothing appears in the queue to tell you it happened.

**What each code does**, which is why this is not a form field for its own sake:

| code | effect on the next run |
|---|---|
| `CANNOT_ACT_NOW` | returns by itself after `SNOOZE_DAYS` (14) — a snooze, not an answer |
| `ALREADY_HANDLED` | stays closed until exposure grows 1.5x |
| `NOT_A_PROBLEM` | stays closed until exposure grows 1.5x |
| `NUMBERS_WRONG` | stays closed; the honest signal that a detector is wrong, not the finding |
| `OTHER` / NULL | exactly today's behaviour |

Required on a rejection (it decides whether the finding ever returns), optional on an approval
(there is nothing to suppress). Every line decided before this column existed has NULL and keeps
behaving precisely as it does now — shipping this does not re-open the back catalogue.

**For `gold_dev`: requested.** It is additive, nullable, and DE owns the DDL.

---

### 1.7 `dim_request_status` is missing most (status x urgency) combinations — **PRODUCTION BUG**

Not a request for a new column. A **live defect in `gold_dev` today**, found on 2026-09-05 while
testing the decision path.

`fact_restock_request.REQUEST_STATUS_KEY` is a FK into `dim_request_status`, which enumerates
(REQUEST_STATUS x URGENCY_LEVEL x DECISION). Any status change has to resolve the new key against
*the line's own urgency*. The dimension does not carry every combination:

| REQUEST_STATUS | urgency levels present in `gold_dev` |
|---|---|
| PENDING_APPROVAL | CRITICAL, HIGH, MEDIUM, LOW |
| APPROVED | CRITICAL, HIGH, MEDIUM, LOW |
| **REJECTED** | **LOW only** |
| **FULFILLING** | **CRITICAL, HIGH only** |
| **COMPLETED** | **HIGH, MEDIUM, LOW** (no CRITICAL) |
| **NEEDS_REVIEW** | **HIGH, MEDIUM only** |

**What it means in production.** `apply_decision` resolves the key with
`SELECT MIN(REQUEST_STATUS_KEY) ... WHERE REQUEST_STATUS = :new AND URGENCY_LEVEL = :urgency`.
A missing combination returns NULL, and the UPDATE dies as
`[DELTA_NOT_NULL_CONSTRAINT_VIOLATED] NOT NULL constraint violated for column:
REQUEST_STATUS_KEY` — an error that names a column rather than the actual problem.

So, on the pipeline as it runs today: **rejecting any line whose urgency is not LOW fails**, and
since `initial_urgency` is usually CRITICAL or HIGH, that is most of them. Completing a CRITICAL
line fails the same way. This has presumably never been exercised — every quote in the demo set
was approved, or rejected while LOW.

**Fixed in the replica** by inserting the 8 missing rows (`DW_SOURCE =
'agentic_restock.fill_status_matrix'`). **DE should populate the full 6 x 4 matrix in `gold_dev`.**
`apply_decision` now also checks the combination up front and fails with a message naming it, so
the next occurrence is diagnosable rather than mysterious.

---

## §2 Replica-only additions

### 2.1 New table `dim_model_bom` — APPLIED 2026-09-04

**Gap.** `manufacturing_analytics.fact_production_execution` carries the production plan
(`PLANNED_QTY` per date/plant/line/**model**). `supply_chain_analytics.dim_bom` is
part→part (`fg_part_id` → `component_part_id`). `dim_part` has no model reference. So there
is **no path from a production plan to a part requirement** — the plan is in vehicle models,
the BOM is in parts, and nothing joins them.

This blocks the BOM-cascade detector, whose whole output is "₹X of <assembly> output at risk",
computed as planned build quantity × the components it consumes.

**Chosen approach.** A bridge table, so no existing table changes shape and the structure
mirrors how automotive BOMs are actually organised (model → top-level assemblies → components):

```sql
CREATE TABLE gold_dev_analytics.supply_chain_analytics.dim_model_bom (
  MODEL_ID          STRING  COMMENT 'FK to dim_vehicle_model.MODEL_ID',
  FG_PART_ID        STRING  COMMENT 'FK to dim_part.PART_ID -- a top-level assembly consumed by this model',
  QTY_PER_VEHICLE   DECIMAL(10,3) COMMENT 'Assemblies required per vehicle built',
  EFFECTIVE_FROM    DATE,
  EFFECTIVE_TO      DATE,
  IS_CURRENT        BOOLEAN
) COMMENT 'Bridge from vehicle model to top-level assembly, so a production plan (expressed in models) can be exploded into part requirements via dim_bom. Added by the Inventory Intelligence project; no equivalent exists in gold_dev.';
```

**Alternatives rejected:** adding `MODEL_ID` to `dim_bom` (overloads a part→part table with a
second grain); treating `MODEL_KEY` as an FG-part proxy (misuses an existing column and makes
the join meaningless to anyone else reading it).

### 2.3 New volume `dataset_staging` — APPLIED 2026-09-04

`gold_dev_analytics.supply_chain_analytics.dataset_staging` (MANAGED). Staging area for bulk
loading the generated dataset: Parquet written locally → uploaded → `COPY INTO`. No UC Volume
existed anywhere in `gold_dev` or `gold_dev_analytics`, and ~300K rows is well past what batched
`INSERT ... VALUES` handles comfortably.

Contents are disposable and regenerated by `scripts/generate_analytics_dataset.py`. Nothing
reads from it at runtime — it is a load path, not a data source. No equivalent is needed in
`gold_dev`.

### 2.2 Dimension pruning — PENDING, data-only, destructive

Not a schema change, recorded here because it is destructive against DE-shaped tables.

Every dimension in the replica is padded to ~1101–1111 rows while the facts reference a small
real core — `dim_vehicle_model` has 1101 rows against **12** distinct `MODEL_KEY` in the
production facts; `dim_warehouse` has 1101 rows but only **3 parts exist at more than one
warehouse** (max 2). The padded members are unreferenced.

Planned: reduce to a realistic core (~10 warehouses, ~100 parts, ~12 models, ~2 plants) and
regenerate facts on top. `DELETE`, not `DROP` — table DDL stays DE's.

**Affected:** `dim_warehouse`, `dim_part`, `dim_supplier`, `dim_plant`,
`dim_production_line`, `dim_vehicle_model`.

---

## §3 Data-quality observations (no change requested)

Surveyed 2026-09-04. These may reflect upstream generator defects rather than intent:

| table | observation |
|---|---|
| `fact_supplier_delivery` | 1100 rows; `DELIVERY_DATE_KEY` and `PLANNED_DATE_KEY` are **`-1` on every row**; `DELAY_DAYS`, `OTD_FLAG`, `FREIGHT_COST`, `DAMAGED_QTY` **100% NULL** |
| `fact_production_execution` | 1110 rows; `EXECUTION_DATE_KEY` **`-1` on every row**; `PLANNED_QTY` **0% populated** |
| `fact_supplier_quality` | 1100 rows with a real date span (2024-01→2026-08), but `INSPECTED_QTY`, `DEFECT_QTY`, `QUALITY_SCORE`, `PPM_LEVEL` **NULL** |
| `fact_inventory_snapshot` | **single snapshot date** (20260902); 1103 rows ≈ one row per part, so effectively no multi-warehouse stocking and no history |
| `fact_inventory_transaction` | 6 months only (2026-03-05 → 2026-08-30), ~13 rows per part |
| `fact_procurement` | 1 row |
| `dim_bom` | 5 rows, flat — **0 multi-level links** (no `component_part_id` appears as another row's `fg_part_id`) |
| all dimensions | padded to ~1101–1111 rows around a much smaller referenced core (see 2.2) |

Two positives worth noting: `dim_date` is fully populated (2923 rows, ~8 years, with
`FISCAL_YEAR/QUARTER`, `IS_WEEKEND`, `IS_HOLIDAY`, `HOLIDAY_NAME`) and is genuinely useful for
seasonality work; `fact_vehicle_build` has real `PRODUCTION_DATE_KEY` values on all 1110 rows.

Also noted: `fact_supplier_delivery.FREIGHT_COST` **exists** as a column (currently NULL).
Project documentation had recorded that no freight or handling cost was available anywhere in
the data, and shaped report wording around that absence — worth revisiting now that the column
is known to exist.

---

## What we are deliberately NOT changing

- `fact_restock_request` — DE owns the DDL; we only write rows. (Its `NOTE` column was added
  out-of-band earlier by `scripts/add_restock_note_column.py`.)
- `fact_plant_capacity` — read by nothing; left empty.
- Table DDL for any pruned dimension — rows only, never the definition.
