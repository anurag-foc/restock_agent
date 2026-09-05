# Intelligence layer redesign — tracker

Single living progress document for the redesign. **Supersedes** the former
`implementation_plan.md` and `phase1_data_plan.md`, which were deleted when this replaced them —
three overlapping planning docs is exactly the drift CLAUDE.md's "Known drift in the working
tree" section exists to warn about. Update this file as work lands.

Design references it points into (these stay):
[pm_day_in_the_life.md](pm_day_in_the_life.md) ·
[intelligence_layer_design.md](intelligence_layer_design.md) ·
[dataset_generator_spec.md](dataset_generator_spec.md) ·
[schema_changes_gold_dev_analytics.md](schema_changes_gold_dev_analytics.md)

---

## Context — why this work is happening

The pipeline claims eight intelligence "nuances" but **structurally can only surface two**.
`rank_priority_actions`' `signal_type` CASE is mutually exclusive by construction
([priority_functions.py:262-266](../src/agentic_restock/jobs/priority_functions.py#L262-L266)), and
`STALLED_COMMITMENT` is a relabel of the other two rather than a third category. The remaining
six nuances — network transfer, seasonality, lead-time drift, supplier reliability, MOQ — were
flattened into columns on a (part, warehouse) row and can never be *the reason* something is
flagged. Lateral transfer, which the market evidence calls "the strongest opportunity" and "the
demo and the first invoice", can only ever cheapen a fix for a candidate that got there another
way.

Three verified defects sit alongside that:

- **Cascade exposure is inflated and its recommendation can be false.** Nothing dedupes parent
  value across children, so N children blocking one assembly each carry that assembly's full
  `value_at_risk`.
- **The worst case in the system is systematically underpriced.** `signal_board.py:291` excludes
  parts below safety stock from the cascade join, so a component that is both short *and*
  blocking A-CRITICAL output is priced at the cost of the parts.
- **`action_cost` is a percentage of exposure that reads as a price.** It is the figure the model
  kept presenting to PMs as real, three times, each time surviving a prose fix.

Intended outcome: detection at each nuance's natural grain, exposure priced as
`P(stockout) × consequence`, real rupee costs from real fix construction, and — for the first
time — detector accuracy that is **measurable** rather than reviewable.

---

## Status at a glance

| phase | what it delivers | state |
|---|---|---|
| **1. Dataset** | the yardstick everything is measured against | ✅ **done and loaded** |
| **2. Estimators** | corrected burn rate; real supplier lead behaviour | ✅ **gate passed** |
| **3. Risk + consequence** | stockout probability, cascade value at risk | ✅ **gate passed** |
| **4. Detectors** | the eight scanners that find problems | ✅ **built** |
| **5. Selection** | rank, diversify, budget to ≤3-4 | ✅ **gate passed** |
| **6. Delivery** | narration, Teams card, review app | ✅ **gate passed** |

**All six phases complete.** 297 tests, every phase behind a measured gate. What remains is
operational: commit, deploy, and run it against the replica end to end.

**The existing pipeline is untouched and still running** on `gold_dev`. All new work targets
`gold_dev_analytics`, so both run side by side — no cutover, and the two can be diffed on the
same day to settle whether decision-value ranking actually picks different actions
(`market_evidence_phase1.md` §16's open question).

---

## Immediate actions

### What the first run against the replica actually produced

278 pairs → **204 findings → 138 after collapse → 4 raised**, four different types. The detection
layer works end to end on data it did not generate at query time.

| finding type | n | best | total |
|---|---:|---:|---:|
| REDEPLOYMENT | 92 | 31.90 cr | 90.74 cr |
| DEAD_CAPITAL | 20 | 7.12 cr | 8.10 cr |
| CASCADE_BLOCK | 1 | 3.25 cr | 3.25 cr |
| LEADTIME_SIGNAL | 8 | 2.43 cr | 4.98 cr |
| DEMAND_SHIFT | 9 | 1.61 cr | 2.70 cr |
| STOCKOUT_RISK | 5 | 0.12 cr | 0.19 cr |
| MOQ_UNECONOMIC | 3 | 0.01 cr | 0.02 cr |

**The diversity constraint is doing the work, as designed.** Ranked purely by money the top five
are four transfers and one dead-capital item. **What the superseded design could have reached at
all** — `STOCK_THRESHOLD` ≈ `STOCKOUT_RISK`, `BOM_CASCADE_RISK` ≈ `CASCADE_BLOCK` — is 3.44 cr of
the ~110 cr now visible. The 31.90 cr transfer and 7.12 cr of dead capital were structurally
unreachable before. Caveat that matters: this is measured on the dataset in this repo, which the
detectors co-evolved with.

#### Bug the run found that 297 tests did not: every transfer was under-sized

`fixes.py` sized a transfer as `ceil(forward_burn × mu_lead) − available` — mean demand over the
lead time, with **no safety term**. Topping up to the mean leaves the receiver at the *median* of
the demand distribution, so residual stockout probability is pinned near 0.50 by construction.
Measured: **75 of 92 transfers landed at exactly 0.50**, while the same report printed
`service_level = 99% (A-CRITICAL)` on its own ASSUMPTIONS line.

Now `mu_dl + z·sigma_dl − available`, using the z the part's criticality class already defines.
Median residual risk **0.499 → 0.074**, and **0 of 92** now sit at 0.50. Three regression tests
added (`test_detectors.py`): the delivered service level must match the promised one, a more
critical part must be topped up further, and an erratic lead time must widen the ask.

Second-order effect worth knowing: REDEPLOYMENT's total value nearly doubled (51.58 → 90.74 cr),
because a transfer that actually reaches the service level removes about twice the risk the
half-sized one did. The fix made the finding *bigger*, not smaller.

#### Reporting fix: dead capital was labelled as money at risk

Its exposure is an **annual carrying cost** — the stock is not going anywhere, it costs that much
every year to hold. It printed as `Rs 7.12 crore at risk`, which reads as an imminent loss. Now
`a year to hold`, agreeing with its own arithmetic trail.

#### Two open questions, deliberately not resolved

1. **The ranking compares a recurring cost against one-time exposures on one axis.** That is how
   a 7.12 cr/yr holding cost outranked a 3.25 cr production block. Naming the unit makes the
   mismatch visible; it does not fix it.
2. **`MOQ_UNECONOMIC` (best 0.01 cr) and `STOCKOUT_RISK` (best 0.12 cr) can never win a slot** at
   a budget of 4. This is the failure the redesign existed to fix, returning as economics rather
   than structure — six nuances could never be *the reason*; now two cannot. MOQ may simply not
   be a finding type: it is a constraint on a purchase, which is what the superseded design had.

#### Dataset skew that shapes all of the above

`part_position` holds 920.6 cr of 928.7 cr total inventory value in **two plant stores**; six
regional DCs hold 8.0 cr between them, and 264 of 278 pairs have a same-part sibling warehouse.
So a transfer is nearly always available and nearly always large, which both inflates
REDEPLOYMENT to ~82% of all value and suppresses STOCKOUT_RISK, since most shortages are absorbed
into a transfer before they can be a shortage. `market_evidence_phase1.md` §2's "lateral transfer
is the strongest opportunity" is therefore **partly confirmed and partly self-inflicted**. Left
as-is on purpose: tuning the warehouse balance to make the numbers read better is exactly how the
dataset and the detectors converge on agreeing with each other.

### Two data changes, and a detector question one of them exposed — 2026-09-05

**P0096 now stocked at WH001** (120 on hand against 600 safety, plus 261 weekdays of issue
history at 28/day). It was the binding child of the P0004 cascade but had **no position row at
the plant at all**, so nothing could be priced and `action_cost` was Rs 0 — which flattered the
finding, since decision value is exposure minus fix cost. Now:

- the cascade quotes a real fix: `fix_cost_all_binding_children` Rs 30.66 lakh, and
  `decision_value` Rs 6.93 crore rather than the full exposure
- P0096 raises its own actionable finding: **buy 1260 units from SUP006 for WH001**, MOQ 60,
  pack size 30, effective unit cost Rs 1,525.59 — which is what reached the live quote

**`fact_procurement.WAREHOUSE_KEY` added and backfilled**, 1119/1119 rows, and
`build_current_position_query` now attributes inbound by warehouse with the plant link as
fallback so the same SQL still runs against `gold_dev`. Correction worth recording: the backfill
came *from* `PLANT_KEY` and was lossless, so the destination was never truly missing in this
dataset — the 27%-vs-5% false-positive gap was the detector not using the link, not absent
information. The column is still right (exact rather than inferred, and the plant trick has no
answer for a PO to a regional DC), but the original §1.3 argument overstated it.

#### The question this exposed: a component with no row does not constrain anything

`cascade.producible()` reads `availability.get(node)`, which is `None` for a part with no
position row, and a `None` child is **skipped from the constraint** rather than treated as zero.
So a component the plant holds none of counts as *not binding* — when it is the most binding
thing there is. That is why adding 120 units of P0096 made `units_blocked` go **up**, 903 →
2009: the part only started constraining once it existed.

Blast radius if the semantics change to `None → 0`: **29 of 176** (BOM component x plant store)
pairs have no position row, so 29 components would begin binding at once, most of them on
high-value assemblies. That is a large, correlated swing in the numbers and it is not obviously
right either — at a warehouse that genuinely does not stock a part, "no row" may mean "not
tracked here" rather than "we have none". Left as-is deliberately; the data fix resolved the
demo case. **Open decision.**

---

### Reason codes — the feedback loop that changes behaviour — 2026-09-05

`DECISION_REASON` (schema_changes §1.6, applied). Required on a rejection, optional on an
approval, and it is the **code** — never the prose — that decides what happens next:

| PM picks | next run |
|---|---|
| Can't act right now | returns by itself after `SNOOZE_DAYS` (14) |
| Already handled | closed until exposure grows 1.5x |
| Not a real problem | closed until exposure grows 1.5x |
| Numbers look wrong | closed; the honest signal that a *detector* is wrong |
| blank (every pre-existing line) | exactly today's behaviour |

The distinction that forced it: **"not a real problem" and "can't act right now" are opposite
signals that read almost identically in free text.** Real notes are "no", "not now", "ask
Rajesh". Treating a deferral as a refusal buries a live problem because someone was busy the
week it was raised — alert fatigue inverted, and worse, because nothing appears in the queue to
say it happened. The dropdown prints each option's consequence beside it, since a PM choosing
between labels with no stated effect is guessing.

Proven against the real replica rows, not just unit tests:

```
day +1   | P0004 (CANNOT_ACT_NOW): quiet   | P0003 (NOT_A_PROBLEM): quiet
day +15  | P0004 (CANNOT_ACT_NOW): RAISED  | P0003 (NOT_A_PROBLEM): quiet
day +40  | P0004 (CANNOT_ACT_NOW): RAISED  | P0003 (NOT_A_PROBLEM): quiet
```

Still recall, not inference: the note is reproduced verbatim and never interpreted, and nothing
here touches the ranking. Rejection *rates* by reason are the tier-2 signal, and they need
months of real decisions — 9 notes exist in the whole table today.

#### A production bug fell out of testing this

`dim_request_status` does not carry every (status x urgency) combination, in `gold_dev` as well
as the replica: `REJECTED` exists only for LOW, `FULFILLING` only for CRITICAL/HIGH. Because
`apply_decision` resolves the new key against the line's own urgency, **rejecting any line that
is not LOW urgency fails today**, as an opaque `DELTA_NOT_NULL_CONSTRAINT_VIOLATED` on
`REQUEST_STATUS_KEY`. Almost every quote line is CRITICAL or HIGH. Fixed in the replica, guarded
with a readable error in the notebook, and written up for DE as schema_changes §1.7.

---

### Fulfillment restructured, and the first feedback loop — 2026-09-05

**Approve now means do it.** A PM approving a line sends it straight to FULFILLING (shown as
*In Progress Actions*) at the proposed quantity; they mark it completed when they have acted,
with a note saying what they did. The second job task is gone: it opened one Supervisor
conversation per approved line and asked `fulfillment_guardrail` to re-check live stock.

That check was **purchase-shaped**. Re-checking stock is meaningful for PURCHASE and TRANSFER
and says nothing at all about RECALIBRATE, REVIEW_STOCK, or the three supplier-grain finding
types — the same mismatch `persist_quote` had. The one case it genuinely caught, a purchase
approved days later that an open PO already covers, is now a deterministic advisory inside
`apply_decision`: same check, no LLM, and non-blocking because the PM has just said to do it.

**The Supervisor is now a one-tool agent.** `genie_agent` went with the detector redesign,
`fulfillment_guardrail` with this. Only `inventory_intelligence_actions` remains. The residual
risk recorded at `build_tool_specs` — that a narration turn could still reach four functions —
is gone: the brief is now the only source of fact the model has, which is the strongest form of
the guarantee this design has been reaching for since the first revision.

Verified end to end on the replica: line 23 (transfer) APPROVED → `FULFILLING` with
`CONFIRMED_QTY = 6043 = REQUESTED_QTY`; line 26 (supplier-grain) REJECTED, note stored.

#### Learning from decisions — tier 1 only, deliberately

`Commitment` now carries `note` and `exposure_at_decision`, and two things follow:

- **Prior decisions are shown back.** Any finding whose subject has decision history gets a
  `PREVIOUSLY DECIDED` block in the brief, quoting the PM's note verbatim, and the agent
  instructions require WHY NOW to acknowledge it and say what has changed. A rejection used to
  remove a finding silently and permanently, so the single most informative thing in the system
  — a domain expert explaining why the machine was wrong, in their own words — went into the
  table and never came out.
- **A rejection re-surfaces when the stakes grow**, at `REJECTION_RESURFACE_MULTIPLE = 1.5`, not
  on a timer. CLAUDE.md recorded this exact gap for as long as `fact_restock_request` had no
  `EXPOSURE_AT_DECISION` to compare against. Lines written before that column existed have NULL
  there and keep suppressing, so shipping this does not re-open the back catalogue.

**This is recall, not inference.** The note is reproduced, never interpreted, and it does not
touch the ranking. Two reasons, both worth keeping:

1. *"Not a real problem"* and *"can't act right now"* are opposite signals that read almost
   identically in free text. Down-weighting on the second teaches the system to stop surfacing
   hard-but-important findings — alert fatigue inverted, and much harder to notice than the
   original. Turning notes into weights needs a **structured reason code**, which is an open
   decision (one field in the app, one column).
2. There are 9 notes in the entire table. At ~8 decisions a day with optional notes,
   per-finding-type rejection rates need months before they mean anything — and on the replica
   there is no real PM feedback at all, so anything demoed there would be synthetic. Tier 1
   works from the very first decision; tiers 2-3 need volume that does not exist yet.

### BLOCKED — the first live run needs one Unity Catalog grant

`2026-09-05`. The pipeline runs end to end and the Supervisor is reached, but `persist_quote`
fails inside the MCP app:

```
RuntimeError: SQL StatementState.FAILED: [INSUFFICIENT_PERMISSIONS]
User does not have USE CATALOG on Catalog 'gold_dev_analytics'
```

The app's service principal is `4d5783b0-8633-4a4e-922a-ccc53730aa7f`
(`app-4n5zny mcp-inventory-actions`). `anurag.r@focaloid.com` cannot grant it — the catalog is
owned by `jithin.joy@focaloid.com` and MANAGE is required. Either the owner runs:

```sql
GRANT USE CATALOG ON CATALOG gold_dev_analytics TO `4d5783b0-8633-4a4e-922a-ccc53730aa7f`;
GRANT USE SCHEMA, SELECT ON SCHEMA gold_dev_analytics.dim TO `4d5783b0-...`;
GRANT USE SCHEMA, SELECT ON SCHEMA gold_dev_analytics.supply_chain_analytics TO `4d5783b0-...`;
GRANT MODIFY ON TABLE gold_dev_analytics.supply_chain_analytics.fact_restock_request TO `4d5783b0-...`;
GRANT MODIFY ON TABLE gold_dev_analytics.supply_chain_analytics.quote_metadata TO `4d5783b0-...`;
```

…or an admin adds that SP to `agentbricks-users`, which already holds ALL PRIVILEGES on both
catalogs and is presumably how `gold_dev` works today.

**Do not infer the SP's access from `SHOW GRANTS`.** `gold_dev` shows no explicit grant for it,
which reads as "it must be in the group" — it is not, and that inference cost a failed run. The
only reliable check is calling a tool and reading the app log.


- [ ] **Commit this work.** Still entirely untracked: 7 docs, ~25 modules across
      `generation/`, `estimators/`, `detectors/` and `jobs/`, 11 test files, the orchestrator
      script and the positions notebook, plus edits to `CLAUDE.md` and
      `docs/market_evidence_phase1.md`. **This is the largest practical risk right now.**
- [ ] **Decide on `WAREHOUSE_KEY` for `fact_procurement`** in the replica. Phase 4 measured the
      cost of not having it: 5x the false-positive rate (27% vs 5%) and 2.4x the finding volume.
      Highest-value change left, and it diverges further from `gold_dev`.
- [x] **Load the dataset** — done 2026-09-04. 300,520 rows across 18 tables in
      `gold_dev_analytics`. Verified against the live tables: 278 (part, warehouse) pairs, 34
      below safety stock, 0 negative balances, 61-day average cover; supplier archetypes
      reproduce their intended behaviour (`tight` σ 1.6 / 91% OTD versus `drifting` σ 2.1 / 2%
      OTD versus `loose` σ 9.5 / 64% OTD).
- [x] **Review app pointed at the replica** — 2026-09-05. Four SQL files switched, plus three
      things the catalog swap alone would not have fixed: `quote_lines`/`fulfilling_lines`
      **inner** joined `dim_part`/`dim_warehouse`, so a supplier-grain line with NULL keys was
      dropped silently (header says 4 lines, screen renders 3); the detail page printed the
      stored zeroes as `0 / 0` stock for a line that has no stock position; and the whole
      approve/reject path (`apply_decision`, `invoke_fulfillment`, the app's `/complete`
      endpoint) was hardcoded to `gold_dev`, so a PM's approval would have matched no rows and
      the job would have reported SUCCESS. `apply_decision` now **fails** when every submitted
      line is NOT_FOUND, which is a wiring error rather than a business state.
- [ ] ~~Point the review app at the replica when the time comes.~~ `AGENTIC_RESTOCK_GOLD_CATALOG`
      covers the Python and notebook paths only — the app's four SQL files under
      `restock-review/config/queries/` name `gold_dev` **literally**, so a parallel run needs
      those edited too. This is why the load was safe: the live app never reads the replica.
- [ ] **Escalate the `fact_procurement` gap to DE.** No actual-receipt date column, in production
      too — so observed lead time is uncomputable from it. `market_evidence_phase1.md` §4 has been
      corrected; DE may simply have receipts in silver that were never promoted.
      (`schema_changes_gold_dev_analytics.md` §1.1)
- [ ] **Also raise the missing `PART_KEY`** on `fact_supplier_delivery` (§1.2) — added in the
      replica, still absent in production.

---

## Phase 1 — Dataset ✅ code complete

300,520 rows across 18 tables, generated deterministically in ~2s. 138 tests. All eight
self-assertions pass, including the audit check that sampled snapshots still tie exactly to the
transaction series.

Modules under `src/agentic_restock/generation/`: `policy` · `entities` · `bom` · `scenarios` ·
`contracts` · `demand` · `replenishment` · `supplier_facts` · `production` · `procurement` ·
`decisions` · `snapshots` · `ground_truth` · `dataset` · `assertions` · `dates`.
Orchestrator: `scripts/generate_analytics_dataset.py` (catalog-guarded, `--prune`/`--load`
separate, refuses to load if assertions fail).

Eleven findings planted and verified findable — including a decoy donor holding **more units but
less cover** than the real donor, two cascade children **co-binding at exactly the same level**,
a part worth ₹12.44 cr as production value but ~₹1.2 lakh as parts, a supplier **on contract on
average with a 14-day spread**, and a ranking-inversion pair.

Policy inputs decided (`generation/policy.py`, rationale in `intelligence_layer_design.md` §5.1):
**holding rate 14%/yr** built bottom-up · **service level tiered 99/95/90%** by criticality ·
**target cover derived** per part as `μ_lead + 7 + z·σ_lead`, not a constant.

- [x] All 13 modules + tests + orchestrator
- [x] Schema changes applied to the replica and documented for DE
- [x] Staging volume `dataset_staging` created
- [x] **Loaded** 2026-09-04, verified against the live tables

### Loader bugs found during the first load, worth knowing before touching it

Four, in sequence. The first three were mechanical; the fourth was silent and dangerous.

1. **`databricks fs cp` needs the `dbfs:` scheme for a Volume destination.** A bare
   `/Volumes/...` path is treated as *local* and fails with a confusing
   `no such directory: /Volumes/...`. SQL reads the volume by its bare path, so the two forms
   genuinely differ and cannot be shared.
2. **Delta rejects a partial INSERT column list** (`DELTA_INSERT_COLUMN_MISMATCH`) — every
   column must be named. The ungenerated ones are DW_* audit stamps, now padded from the
   target's own `information_schema`: timestamps get `current_timestamp()`, everything else
   `CAST(NULL AS <type>)`.
3. **An all-NULL column has no inferable type.** pyarrow writes it as a null field, Spark reads
   it back as INT, and the insert fails on `cannot cast INT to DATE` — which an explicit CAST
   does *not* fix, because Spark refuses INT→DATE outright. Such columns are now written as
   strings, which casts cleanly to any target type.
4. **The staged schema came from `rows[0]`.** `sim_ground_truth` holds two row shapes — PAIR
   rows with burn parameters, SUPPLIER_PART rows with lead-time parameters — so every
   supplier-only column was dropped and then padded with NULL. **The load reported success, all
   340 rows were present, and the half of Phase 2's gate that grades the lead-time estimator was
   blank.** Row counts cannot detect this. Fixed at `dataset.all_keys()` (union across all rows),
   and assertion **5.8 ground truth is gradeable** now fails the load if any column the gate
   grades against is null on every row of its subject type.

---

## Phase 2 — Estimators ✅ gate passed

- [x] `estimators/burn.py` — E1: intermittency tested first, else level × shrunk seasonal index
- [x] `estimators/leadtime.py` — E2: hierarchical tiers, ~180-day recency half-life
- [x] `jobs/positions.py` + `notebooks/positions/refresh_positions.py`
- [x] `part_position`, `supplier_performance` — built and verified against the live tables
- [x] `tests/test_ground_truth_recovery.py` (the gate), `tests/test_positions.py`

**184 tests.** Gate results, graded against the *observable* ground-truth columns:

| regime | n | median | p90 | | E2 | |
|---|---|---|---|---|---|---|
smooth | 81 | **1.3%** | 3.1% | | sigma at pair tier | **0.002%** max error |
seasonal | 62 | **2.0%** | 4.1% | | tier mismatches | **0 of 62** |
trending | 23 | 5.0% | 12.2% | | tiers exercised | pair 15 · supplier 34 · category 13 |
step | 23 | 6.6% | 10.2% | | | |
erratic | 31 | 9.1% | 18.4% | | | |
intermittent | 52 | **0.7%** | 3.5% | | | |

Routing: 52 Croston · 222 level×season · 3 trailing mean · 1 stopped. Confidence differentiates
(71 HIGH / 195 MEDIUM / 12 LOW) with every sparse-history pair correctly LOW.

### What the gate caught — two of them in the gate's own targets

1. **Trending scored 14%** against a full-window-mean target, which lags a trending series. The
   target is now measured off the realised segment with a midpoint→today correction. → 5.0%.
2. **The tier expectation disagreed on 28 of 62 pairs**, because it was derived from the pair
   count alone — but the supplier pool spans that supplier's other parts and is often rich where
   a pair is sparse. The ladder does not degrade monotonically with the pair count. Now computed
   from all three pool sizes, with thresholds imported from the estimator rather than restated.
3. **Croston reported a healthy burn for stock that had stopped moving** — it averages the whole
   window, so a dead-capital pair showed 3.35/day and ~40 days of cover instead of stock that
   will never be consumed. Added a staleness branch keyed to multiples of the part's *own* demand
   interval; a fixed day count would call a slow mover dead.
4. **The `untested` supplier archetype still had delivery history**, so E2's ladder never fell
   past the supplier tier. It now genuinely has none, which took `supplier_category` from 1 pair
   to 13.

### Two deliberate judgements

**`contracted` is no longer required to appear in the dataset.** With 744 captured deliveries
there is always *some* pool, and using it is correct — falling through to the contract while data
exists would be worse. It is a genuine cold-start path, unit-tested directly. Requiring it would
mean degrading the dataset to satisfy an assertion, and the old assertion passed vacuously on an
empty list anyway.

**Estimation runs on the driver, not `applyInPandas`.** At ~280 pairs it takes about a second and
the code stays identical to what the local gate tests. `densify_issues` and the per-pair
`estimate_burn` call are already per-group pure functions, so the move to
`groupBy().applyInPandas()` is a one-line change when volume needs it — doing it now would buy
nothing and cost the local gate.

### New schema gap found

**`fact_procurement` cannot attribute an open PO to a receiving warehouse** — it has `PLANT_KEY`
but no `WAREHOUSE_KEY`, so inbound reaches a warehouse only via `LINKED_PLANT_ID`, which regional
DCs lack. Their availability is understated. Summing a part's inbound across its warehouses would
be worse (same order counted per location). Recorded as request §1.3; current behaviour joins
through the plant link and accepts zero for RDCs.

---

## Phase 3 — Risk and consequence ✅ gate passed

- [x] `estimators/cascade.py` — C: recursive explosion, computed at the parent, value counted
      once, returns the binding **leaf** set
- [x] `estimators/risk.py` — R: `P(stockout) × consequence`
- [x] `build_parent_cascades` / `attach_exposure` in `jobs/positions.py`
- [x] `tests/test_cascade_and_risk.py` — **209 tests total**

**Gate results:** 24 cascades at production sites only, 3 blocked, **₹23.01 cr counted once** ·
F2's binding set is exactly the planted `P0041,P0069` (each carrying ₹6.55 cr, with the other
named as co-binding) · F3's is exactly `P0042` · **F3 reprices 938×** — ₹10.96 cr of production
value against ₹1.17 lakh as parts.

Exposure correlates only **0.745** with the superseded `shortfall × unit_cost`, so the two
genuinely order differently — the beginning of an answer to §16.

### Four bugs, each of which disarmed a finding silently rather than failing

1. **The cascade was flattened, not recursive.** Every descendant was pre-multiplied against the
   parent and one global MIN taken — which treats a sub-assembly and its own components as
   independent constraints when they are *additive* for that branch (10 sub-assemblies on hand
   PLUS 5 more buildable from components = 15). It reported the sub-assembly as binding, so the
   components a PM would actually buy never surfaced.
2. **Parent stock absorbed the block.** Supply is `parent_on_hand + buildable_from_children`, so
   an assembly holding 1785 units covered the shortfall and its children stopped binding.
   Correct arithmetic, but it disarmed both cascade findings.
3. **Then the intermediate sub-assembly absorbed it**, one level down, for the same reason. The
   dataset now caps own stock along the whole path, derived from the BOM rather than listed so
   re-parenting a component cannot silently disarm the finding again.
4. **Cascades were assessed at regional DCs**, which never build anything — 51 blocked parents
   worth ₹618 cr, almost all fictional. Restricted to `PLANT_STORE` warehouses.

Plus a fifth caught by its own test: `descend` stopped at a node holding more stock than its
children could add, reporting the sub-assembly as the action. Own stock is *fixed*, so raising
capacity means buying further down; it now descends to the leaf unconditionally.

### Deviation from the design doc

**No `norm_cdf` UC function.** The design put scanners in SQL and sketched an Abramowitz-Stegun
approximation. Since Phase 2's estimators ended up in Python — which is what makes the accuracy
gate a local test rather than a cluster job — `math.erf` gives Φ exactly and the approximation
would only add error. If scanners later move to SQL, the UC function comes back.

**F11's ranking inversion moves to Phase 4.** It compares *decision value*, which needs
`action_cost` from the fix constructors. Testing it here would mean building a throwaway cost
model.

---

## Phase 4 — Fixes and detectors ✅

- [x] `detectors/fixes.py` — FX1 (transfer, using the now-populated `FREIGHT_COST`), FX2
      (`effective_unit_cost`), FX3 (orderable qty — **computed, never chosen**)
- [x] `detectors/findings.py` — the common shape, `evidence` + `assumptions_used`
- [x] `detectors/scanners.py` — S1-S8, each at its own grain
- [x] `tests/test_detectors.py`

**All 8 finding types fire.** Every planted finding fires (F1-F8 confirmed; the earlier "21/33
recall" was a mis-specified metric — it marked *every pair of every named part* as expecting a
pair-level finding, including F1's healthy donor warehouses and parts whose finding lives at
supplier grain).

### The §16 question, answered — and the answer is partly negative

Ranking by decision value against raw exposure: **Spearman 0.577**, **240 of 286 findings move
≥10 places** — but the **top 3, 5 and 10 are identical**, with divergence starting only at
top-20. The cause is structural: at the top, exposure runs ₹8-18 cr against fix costs of
₹0-10 lakh, so subtracting the cost cannot reorder anything. At a 3-4 output budget the top is
all a PM sees, so **decision-value ranking would surface the same actions as raw exposure.**

Found only after pricing the cascade fix, which removed the obvious artifact explanation. The
claim "ranking by money-after-cost picks different actions" should be retired from the pitch. The
real wins are the eight finding types and the cascade correctness. What *does* change the output
is the diversity constraint, which is why Phase 5 puts its care there.

### The schema gap, quantified

Running the scanners with three different inbound attributions:

| inbound credited | findings | false shortages on quiet pairs |
|---|---|---|
| none | 286 | 116/237 (49%) |
| **plant stores only — today's schema** | **183** | **63/237 (27%)** |
| complete — needs `WAREHOUSE_KEY` | **76** | **12/237 (5%)** |

**5× the false-positive rate and 2.4× the volume.** Recorded in schema_changes §1.3 with the
numbers; applying `WAREHOUSE_KEY` to the replica is the highest-value change left, and needs a
decision because it diverges further from `gold_dev`.

### Five bugs, all producing plausible output rather than failing

1. **The variance premium was a constant** — divided by the buffer it was pricing, cancelling
   `sigma_lead`. F5 could never fire. (My own comment warned against exactly this, then did it.)
2. **F5's data did not support a reversal** — a 25% price gap is not offset by a 5.8% reject
   rate, so the cheap supplier genuinely *was* cheapest. Real competing quotes differ by a few
   percent; at 1.00/1.03/1.045 the reversal is real.
3. **The consequence proxy scaled with holdings** — ₹218 cr for one part, because holding more
   stock increased what it cost to run out. Now scales with flow.
4. **S1 scanned in-house assemblies**, which have no contract, putting eight of them atop the
   ranking with `action_cost = 0`.
5. **The cascade fix was unpriced**, holding `decision_value == exposure` for the largest
   findings in the set.

**And a specification error of mine: F9's premise was wrong.** Those spares are flagged as dead
capital on 2,431 days of cover — 6.6 years of stock on a slow mover, which is correct and
valuable. The claim should be that lumpy demand is not mistaken for an imminent *shortage*, not
that it yields no finding. Corrected; false shortages there went from 8/8 to 1/8.

---

## Phase 5 — Selection ✅ gate passed

- [x] `detectors/selection.py` — suppression, duplicate collapse, diversity, hard budget
- [x] `tests/test_selection.py` — **256 tests total**

**Gate:** 286 scanned → 277 after suppression → 188 after collapse → **4 selected, all four a
different finding type**:

| | finding | value |
|---|---|---|
1 | transfer 2,249 units of P0012 between plants | ₹18.1 cr |
2 | unblock P0002 — needs P0042 | ₹10.9 cr |
3 | 12,113 units of P0003 on 225 days of cover | ₹7.1 cr |
4 | SUP010 — on contract on average but unpredictable | ₹2.45 cr |

The superseded design would have shown two or three stock thresholds. Assumption disclosure works
per finding: the transfer discloses only `service_level`, the cascade `cascade_horizon` +
`holding_rate`.

### Two defects found here

**Diversifying by type is not sufficient.** `CASCADE_BLOCK P0002: needs P0042` and
`STOCKOUT_RISK P0042: buy 4601 units` are the same problem from two ends, and they took two of
four budget slots. `collapse_duplicates` now merges a cascade with its binding children's
shortages, and merges a transfer with a purchase on the same pair into one finding carrying the
other as the alternative considered — which is also what a report needs to show a rejected
option rather than assert one existed. Collapsing removed 88 duplicates.

**`COMPLETED` suppressed forever.** `suppresses()` returned `not is_stale(...)`, and a terminal
status is never stale — so a part whose last order completed 60 days ago could never be raised
again. After a few months of operation that would have muted most of the catalog silently. Caught
by its own test.

`STALLED_COMMITMENT` is modelled as a **flag on a finding**, not a finding type — the superseded
design made it one of three `signal_type` values, which is how the system claimed three
categories while having two.

---

## Phase 6 — Narration and delivery ✅ gate passed

- [x] `narration.py` — the brief: evidence, output skeleton, and **the arithmetic already
      performed**
- [x] `jobs/run_intelligence.py` — one Supervisor turn, replacing the 2+N protocol
- [x] `notebooks/lakeflow_trigger/run_intelligence.py`
- [x] `resources/jobs/intelligence_job.yml` — bundle validates; added to
      `ensure_supervisor_agent.py`'s `JOB_YAMLS`
- [x] `ensure_supervisor_agent.py` reconciled to **two** tools
- [x] `IntelligenceReport.tsx` accepts both assumption labels; typechecks clean
- [x] **Assumption disclosure rendered in the Review App** — the brief's `ASSUMPTIONS USED`
      line parses into one row per policy input, each tagged `policy` or `measured`, with its
      basis beside it. The distinction is the point: a *measured* input is arguable against the
      data, a *policy* one is a choice someone made and can be changed. Anything that does not
      match the shape — every quote written before this — falls back to the raw line rather than
      rendering empty. The Teams card stays free of all of it; it is a nudge, not the report.
- [x] `tests/test_narration.py` (25) + `tests/test_run_intelligence.py` (16)

### The gate: each documented fabrication, prevented structurally

Every one of these had prose forbidding it, in the agent's instructions, **at the time it
happened**. The tests assert the brief contains no slot the wrong value could go into, rather
than that an instruction is present:

| failure that reached a PM | what now prevents it |
|---|---|
`Rs 1,700 holding` against a real `excess_qty: 0`, twice, after two prose fixes | the holding clause is **absent** when excess is zero — while the evidence dump still records the real `0.0`, because a row of zeroes is data, not absence |
a total 5.6x out, stated before its own breakdown | trail runs left to right, total last; a test asserts it actually sums |
`action_cost` printed as a price a PM cannot check | `DECISION VALUE` never prints it; a costless action states one figure, not two identical ones |
`Rs 17,280 (80 x Rs 216)` back-solved for a transfer — `Rs 216` existed nowhere | a transfer with no freight data quotes **no rupee figure**; with real data it may, since the constraint was the absent column, not a rule about transfers |
"no drift data for P1002" when the call returned a row | evidence is a verbatim dump; a zero-valued field is printed rather than omitted |
"Transfer 150 units from SUP-040" for a purchase | recommendation verb must match the action type — a supplier is bought from, a warehouse transferred from |
a full report with **zero** part-lines, job reporting SUCCESS | tool arguments pre-resolved to PART_IDs; a missing `persist_quote` is a hard failure |

### Two corrections to earlier claims

**"3 tools → 1" was wrong; it is 3 → 2.** The same Supervisor serves the fulfillment path, which
genuinely needs `fulfillment_guardrail`. Only the 23-function analysis space goes. Residual risk
recorded at `build_tool_specs`: the guardrail stays reachable during narration, but its four
functions are all already printed in the brief, so a call can at worst confirm it rather than
substitute a different answer.

**Running `ensure_supervisor_agent.py` is now the migration step**, not merely a check — the
reconciler deleting anything outside its spec is what actually removes `genie_agent` from the
deployed agent.

---

## Superseded Phase 6 plan

- [ ] `narration.py` — build the prompt from `evidence_json`, one non-agentic call, strict
      template. Grounding rules from `SUPERVISOR_INSTRUCTIONS` carry over with far less surface
      area: nothing to select, nothing to fetch, no self-chosen arguments
- [ ] `notebooks/lakeflow_trigger/run_intelligence.py` — replaces `invoke_supervisor.py`'s
      2+N-turn protocol
- [ ] `resources/jobs/intelligence_job.yml`
- [ ] Persist/notify via the existing MCP tools, unchanged
- [ ] `restock-review/.../IntelligenceReport.tsx` — parse the new artifact **and keep the existing
      parsers**, so older quotes still render
- [ ] **Assumption disclosure in the Review App** — inline arithmetic trail plus a compact block
      naming the policy values used. Teams card stays free of them

**Gate:** an end-to-end run on the replica produces a quote where every figure traces to an
`evidence_json` field, and the review app renders it.

Do **not** assume the documented fabrication modes are gone — re-test them explicitly.

---

## Open decisions

- [ ] **Fulfillment path.** `invoke_fulfillment.py` + the `fulfillment_guardrail` Genie space
      still depend on 4 legacy UC functions. If Genie leaves the analysis path, this is the one
      place it stays load-bearing. Deferrable past Phase 5, not past Phase 6.
- [ ] **Narration runtime.** A Foundation Model serving endpoint called from Python is my
      recommendation. `ai_query` over `inventory_findings` is tempting and remarkably simple, but
      a strict multi-section template is awkward to control from SQL.

---

## Retirement — only after Phase 6 passes on real runs

Nothing is deleted before then. `inventory_signal_board` → superseded by `part_position` +
`parent_cascade` · the 8 phase-1 UC functions → superseded by S1-S8 · `rank_priority_actions*` →
superseded by `selection.py` · `genie_agent` → off the analysis critical path, kept for ad-hoc
human exploration · the 4 functions `fulfillment_guardrail` uses → **keep** until that decision
· `scripts/seed_demo_scenarios.py` → keep, for `gold_dev`.

---

## Risks

1. **Phase 2's gate fails.** The likeliest single point of failure, and why it is early. It is a
   measured gate with a tuning iteration budgeted, not a review.
2. **Narration is still an LLM.** Surface area shrinks a lot but the grounding rules still apply.
3. **Estimator cost at cadence.** Reading 36 months of history twice a day is the part to watch.
   If it bites, split the cadence — E2 and seasonal indices are slow-moving.
4. **`consequence` for parts outside a planned assembly** stays the weakest number in the system
   (`intelligence_layer_design.md` §6.1). Label it; do not dress it as precision.

---

## Verification

```bash
uv run pytest -q                      # 138 tests, ~20s
uv run ruff check src scripts tests   # not `ruff check .` — notebooks/ is not ruff-clean by design

PYTHONPATH=src python3 scripts/generate_analytics_dataset.py --report    # counts + coverage
PYTHONPATH=src python3 scripts/generate_analytics_dataset.py --dry-run   # runs the §5 gate, writes nothing
```

Per-phase verification is the gate stated in each section above, run against `sim_ground_truth`.
End to end, once Phase 6 lands: point `AGENTIC_RESTOCK_GOLD_CATALOG=gold_dev_analytics` at the
pipeline, run it, and confirm the review app renders a quote whose every figure is traceable.
