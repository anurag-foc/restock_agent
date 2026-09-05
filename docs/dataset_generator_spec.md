# Dataset generator spec — `gold_dev_analytics`

**Principle: the finding catalog comes first.** We are not generating realistic data and
hoping signals emerge. We decide which findings the system must produce, then construct the
data conditions that produce exactly those — plus a quiet majority that produces nothing.

Two consequences that shape everything below:

1. **Generate the process, never the answer.** Current seeding back-solves totals so a ratio
   lands on a target (`seed_demo_scenarios.py` says as much: "arithmetic, not per-day
   realism"). That cannot produce a variance, a seasonal index, or an intermittency rate — and
   three of the seven detectors need exactly those. So: simulate daily demand from a known
   generative model, simulate delivery timing from a known distribution, and let the statistics
   fall out.
2. **The dataset must reconcile.** `on_hand[t] = on_hand[t-1] − issues[t] + receipts[t]` must
   hold for every pair, every day. This is the property that makes the whole dataset auditable
   — anyone can check the snapshot series against the transaction series — and it is what
   separates a generated process from planted numbers.

Dataset "today" = **2026-09-04**. History spans **2023-09-01 → 2026-09-04** (36 months).

---

## §1 The finding catalog — what the system must produce

Each row is a finding we want on screen, the data condition that creates it, and what having
it proves. `F8` and `F11` are additions beyond the original seven nuances — flagged as such,
not smuggled in.

| id | the finding | data condition to construct | what it proves |
|---|---|---|---|
| **F1** | *Transfer instead of buying* — part short at WH-A, donor-protected surplus at WH-C | same part at 5 warehouses: one starving, one rich, three neutral. **The biggest pile is deliberately not the best donor** — that warehouse's own burn is high, so its cover is thin | nuance 1, the highest-ROI action in the evidence base (§2). Donor *ranking* is visibly non-trivial, not "find any surplus" |
| **F2** | *Cascade blocked by two components* — an A-CRITICAL model's build plan blocked by **two** parts; the recommendation must name both | one model, 6 components, two of them co-binding at similar shortfall | nuance 2 done correctly. "Fix one, parent unblocked" is false, and this is the case that exposes it |
| **F3** | *The underpriced worst case* — a component **both** below safety stock **and** cascade-binding | one part configured to satisfy both conditions at once | the exposure fix. Priced at parts cost it looks trivial; priced at production value it is the top finding. Demonstrable side by side |
| **F4** | *Variance, not drift* — supplier on contract at 40 days mean, **σ = 14** | 18 deliveries from N(40, 14) for SUP-A; contrast SUP-B at N(47, 3) — real drift, low spread | nuance 5 measures the right quantity. Zero drift, unmanageable supplier — invisible to any mean-only check |
| **F5** | *The cheap supplier is expensive* — lowest quoted price has worst rejects and highest σ_L, so `effective_unit_cost` **reverses** the ranking | one part, 3 contracted suppliers: prices 100/112/125, reject rates 6%/1%/0.5%, σ_L 12/4/2 | nuance 6's entire value proposition. A score of 62 is unarguable; a reversed cost ranking is not |
| **F6** | *MOQ makes the fix uneconomic* — need 60, MOQ 500, ~8 months overbuy → recommend renegotiating the pack, not placing the order | contract with MOQ 500 on a part burning ~2/day | nuance 7 producing a **different action type**, not a footnote on a buy |
| **F7** | *Demand shifted, safety stock didn't* — sustained 1.6× increase over 4 months against a safety stock set for the old rate | step change in the demand process 120 days before "today", safety stock left at the pre-step value | nuance 4 as a finding. Requires real daily history — a sustained shift must be distinguishable from one big issue |
| **F8** | *Dead capital* — 400 days of cover, zero issues in 180 days, ₹X trapped **(addition)** | 6 pairs with high on-hand and a demand process switched off 180+ days ago | the excess half of §2 (38% of inventory is excess). The original seven nuances only ever detect *shortage*; this is the same network view pointed the other way, and it is the working-capital pitch |
| **F9** | *Intermittent part, correctly quiet* — 85% zero-demand days, daily mean is nonsense, detector must **not** fire | 8 spare parts on a Croston-style process: issue interval ~12 days, size ~4 | nuance 4's correctness. A negative test — proves sophistication by what it declines to flag |
| **F10** | *Quiet majority* — ~70% of pairs produce nothing | healthy cover, on-contract suppliers, no cascade exposure | the alert-fatigue thesis (§3). "Quiet on 8 of 14 runs" is only claimable if quiet is the normal case |
| **F11** | *Ranking inversion* — raw exposure and decision value **disagree on the order** **(addition)** | Part X: ₹50L exposure, transfer fix (cost ≈3%) → DV ≈ ₹48.5L. Part Y: ₹60L exposure, only a 60-day-lead buy (cost ≈38%) → DV ≈ ₹37L. Exposure says Y first; decision value says X first | closes §16's open validation item — *does* decision-value ranking differ from raw exposure on real data. This dataset can answer it instead of arguing it |

### Lead-time fallback tiers (needed or the hierarchy is untestable)

[5] falls back (supplier,part) → (supplier) → (category) → contracted. All four tiers must be
reachable, so delivery history is deliberately uneven:

| tier | construct | count |
|---|---|---|
| rich | ≥12 deliveries per (supplier, part) | 8 pairs |
| medium | 3–5 deliveries — pair tier unusable, supplier tier works | 10 pairs |
| sparse | 1–2 deliveries — both pair and supplier tiers unusable | 12 pairs |
| none | contracted lead time only, zero delivery history | 10 pairs |

---

## §2 Entity inventory

Replaces the ~1101-row padded dimensions (see `schema_changes_gold_dev_analytics.md` §2.2).

| dimension | target | notes |
|---|---|---|
`dim_warehouse` | **10** | 2 plant-attached + 8 regional. 8 ACTIVE, 1 UNDER_MAINTENANCE, 1 PLANNED — the non-ACTIVE two exist to prove the board's status filter works |
`dim_part` | **100** | 12 finished/top-level assemblies · 28 sub-assemblies · 60 components. `BOM_LEVEL` set truthfully; `UNIT_COST` spanning ₹40 → ₹1,25,000 (3+ orders of magnitude, so ranking has to discriminate on value not quantity); `CRITICALITY_CLASS` mix incl. both existing spellings of `A-CRITICAL` **only if** we want to keep exercising that normalisation bug — otherwise clean |
`dim_vehicle_model` | **12** | matches the 12 already referenced by the production facts |
`dim_supplier` | **25** | archetypes per §3.3 |
`dim_plant` | **2** | |
`dim_production_line` | **6** | 3 per plant |
`dim_date` | keep rows, add holidays | verified 2023-01-01 → 2030-12-31, `FISCAL_*`/`IS_WEEKEND` populated; `IS_HOLIDAY` is empty and gets populated by the generator (§7.3) |

**(part, warehouse) pairs: ~270.** 40 focus parts stocked at 4–5 warehouses (~180 pairs) +
60 background parts at 1–2 (~90 pairs). Enough that ranking must discriminate; small enough
to hand-verify a full run.

---

## §3 Generative processes

### 3.1 Demand → `fact_inventory_transaction` (ISSUE)

Per (part, warehouse), draw daily quantity from one of five regimes. Regime assignment is
**deterministic by sorted position**, never random, so a rebuild reproduces the dataset.

| regime | model | share |
|---|---|---|
smooth | `level × noise`, CV ≈ 0.15 | 30% |
seasonal | `level × season(t) × noise` where `season` is driven by `dim_date`'s `FISCAL_QUARTER` plus the `IS_HOLIDAY` flags the generator populates (see §7.3 — they are empty today) — festive-season and fiscal-year-end ramps, repeated across all 3 years so an index is estimable | 25% |
trending | `level × (1 + g·t) × noise` | 10% |
**step** | level jumps ×1.6 at a fixed date (F7) | 8% |
**intermittent** | Croston: interval ~ Geom(1/12), size ~ Pois(4) → ~85% zero days (F9) | 15% |
erratic | CV ≈ 0.9, no structure — should be flagged low-confidence, not acted on | 12% |

Weekends/holidays suppress issues for plant-attached warehouses (real calendar, from
`dim_date`).

**History depth — orthogonal to regime, and required.** E1 shrinks its seasonal index by
`min(years_of_history / 3, 1)`, which is exactly `1.0` at 36 months. If every pair has the full
history, **no pair ever exercises shrinkage or the LOW-confidence path** and two of E1's three
branches are dead code. So depth is varied deliberately:

| depth | pairs | exercises |
|---|---|---|
full 36 months | ~245 | seasonal index at full weight |
6–18 months | ~10 | shrinkage active (`< 2 years`) |
< 90 days | ~5 | no seasonality claimed, `burn_confidence = LOW`, and R's widened `σ_DL` pulling `P_stockout` toward 0.5 |

The short-history pairs also matter for the report: they are the cases where the honest output
is "we do not know this part well enough to be confident", which the system should be able to
say.

### 3.2 Stock position → `fact_inventory_snapshot`

Daily per pair, 36 months, **derived from the demand series, not independent**:

- Start at an opening balance; each day `on_hand -= issues`, `+= receipts` when a simulated
  replenishment lands.
- Reorder behaviour: when `on_hand` crosses `SAFETY_STOCK_QTY`, raise a replenishment that
  arrives after that supplier's *sampled* lead time (this is what makes stockouts emergent
  rather than planted).
- `SAFETY_STOCK_QTY` / `MAX_STOCK_LEVEL` set from the **pre-step** demand rate, so F7's
  mis-calibration is real rather than asserted.
- `AVG_DAILY_CONSUMPTION` written as the naive trailing mean — deliberately the *wrong*
  number, so a detector using it instead of the corrected burn is visibly worse.
- **Reconciliation assertion** (§5) must pass.

Volume ≈ 270 × 1095 ≈ **295K rows**. Trivial for Delta, and daily granularity is what makes
the reconciliation check possible.

### 3.3 Supplier behaviour → `fact_supplier_delivery`, `fact_supplier_quality`

25 suppliers across archetypes, each with fixed true parameters:

| archetype | μ_L vs contract | σ_L | reject rate | n |
|---|---|---|---|---|
tight | on contract | 2 | 0.5% | 6 |
**loose (F4)** | **on contract** | **14** | 2% | 3 |
drifting | +7 | 3 | 2% | 4 |
improving | +9 early, +1 last 6 months | 4 | 3% | 3 |
**cheap-and-bad (F5)** | +5 | 12 | 6% | 3 |
untested | — | — | — | 6 (little/no history) |

Populate `DELIVERY_DATE_KEY`/`PLANNED_DATE_KEY` (currently `-1` on every row), `DELAY_DAYS`,
`OTD_FLAG`, `DAMAGED_QTY`, `SHORT_QTY`, the **new `PART_KEY`**, and `FREIGHT_COST` — the last
one because the column exists and populating it lets a transfer quote a real cost instead of
describing the downside in words.

`fact_supplier_quality`: populate `INSPECTED_QTY`, `DEFECT_QTY`, `QUALITY_SCORE`, `PPM_LEVEL`,
`COST_OF_POOR_QUALITY` (all currently NULL) consistent with each archetype's reject rate.

### 3.4 Production plan → `fact_production_execution` + `dim_model_bom` + `dim_bom`

- `fact_production_execution`: populate `EXECUTION_DATE_KEY` (currently `-1`) and
  `PLANNED_QTY`/`ACTUAL_QTY` per (date, plant, line, model). **This is the build-target source
  that retires the `MAX_STOCK_LEVEL − QUANTITY_ON_HAND` proxy.**
- `dim_model_bom`: 12 models → top-level assemblies, `QTY_PER_VEHICLE`.
- `dim_bom`: **3 levels** — assembly → sub-assembly → component, so recursive explosion is
  exercised. Currently 5 flat rows with zero multi-level links. Include F2's co-binding pair
  and F3's dual-condition part.

### 3.5 Contracts and inbound → `dim_supplier_contract`, `fact_procurement`

- Contracts grown from 38 to ~60. MOQ/pack regimes spread deliberately: MOQ ≪ need (no
  constraint) · MOQ ≈ need · **MOQ ≫ need (F6)** · awkward pack (need 100, pack 30).
- `fact_procurement` (1 row today) = **inbound pipeline only**: ~50 open POs with
  `STATUS`/`PENDING_QTY`/`EXPECTED_DATE_KEY`, feeding `on_order_arriving_within_L`. It cannot
  serve lead-time history — no receipt-date column exists (see
  `schema_changes_gold_dev_analytics.md` §1.1).

---

## §4 `sim_ground_truth`

The table that makes detector quality *measurable* rather than eyeballed. One row per
(part, warehouse) plus one per supplier, recording the parameters we generated with:

```
subject_type · subject_id · regime · true_level · true_seasonal_amplitude
· true_noise_cv · true_intermittency_rate · step_date · step_factor
· true_mu_lead · true_sigma_lead · true_reject_rate
· planted_finding_ids (array)  -- e.g. ['F1','F11']
· expect_finding BOOLEAN       -- false for the quiet majority
```

With this, three things become measurable that currently cannot be: whether [4] recovers the
true burn, whether [5] recovers true σ_L, and **detector precision/recall** — how many pairs
with `expect_finding = false` produced a finding anyway. That last number is the alert-fatigue
claim, quantified.

(The retired `sim_pair_scenarios` was reaching for this; reviving it deliberately.)

---

## §5 Generator self-assertions

The script fails loudly rather than producing a subtly wrong dataset:

1. **Reconciliation** — for every pair/day, `on_hand[t] == on_hand[t-1] − issues[t] + receipts[t]`.
2. **Every finding is live** — each of F1–F11 produces at least one detectable finding on the
   2026-09-04 board. A finding catalog that silently stops firing is the failure mode this
   whole spec exists to prevent.
3. **Quiet majority holds** — ≥65% of pairs produce no finding.
4. **Regime coverage** — every demand regime and supplier archetype has ≥3 members, so no
   detector branch is dead code.
5. **Fallback tiers populated** — all four [5] tiers reachable.
6. **Referential integrity** — no fact row references a pruned dimension member; no `-1`
   sentinel dates remain in any table we populate.
7. **Determinism** — two runs from the same seed produce byte-identical row sets.

---

## §6 Volumes

| table | rows | note |
|---|---|---|
`fact_inventory_snapshot` | ~295K | 270 pairs × 1095 days |
`fact_inventory_transaction` | ~250K | ISSUE + RECEIPT |
`fact_production_execution` | ~13K | 12 models × 1095 days |
`fact_supplier_delivery` | ~1.1K | uneven by design (§1 tiers) |
`fact_supplier_quality` | ~1.1K | one inspection per delivery |
`fact_procurement` | ~50 | open POs only |
`dim_bom` | ~120 | 3 levels |
`dim_model_bom` | ~40 | |
`dim_supplier_contract` | ~60 | |
dimensions | ~155 total | down from ~6,600 padded rows |

Well inside a single serverless SQL run.

---

## §7 Decisions

Resolved 2026-09-04.

### 7.1 Decision history — synthesize, as a byproduct of the simulation

`fact_restock_request` is regenerated, **not** started empty. Without open commitments,
nuance-8 suppression is dead code and `STALLED_COMMITMENT` can never fire — leaving only two
live signal types, which is the exact collapse this redesign exists to fix.

The method matters: §3.2 already simulates a reorder-and-arrive loop, so **a fraction of those
simulated replenishments are recorded as restock requests with decisions** rather than being
invisible. The history is then automatically consistent with the stock series — an approval
corresponds to a real stockout risk on that date, and its receipt already appears in the
transaction series because the reconciliation invariant (§5.1) forces it. A separately-invented
history would reference stock positions that never existed.

Status matrix to cover, so every suppression branch is exercised:

| status | age | expected behaviour |
|---|---|---|
`REJECTED` | any | permanently suppressed |
`PENDING_APPROVAL` | < 2 days | suppressed (fresh) |
`PENDING_APPROVAL` | > 2 days | re-surfaces as `STALLED_COMMITMENT` |
`APPROVED` / `FULFILLING` | < lead + 3 | suppressed (in execution) |
`APPROVED` / `FULFILLING` | > lead + 3 | re-surfaces as `STALLED_COMMITMENT` |
`COMPLETED` | historical | feeds the decision ledger, no suppression |

~6 months of history, several lines per month, plus live `PENDING_APPROVAL` lines so the review
app's pending queue and Fulfilling Orders page both render with real content.

### 7.2 `A-CRITICAL` spelling split — inject deliberately, ~30% of rows

Kept. The board's `REPLACE(CRITICALITY_CLASS, ' ', '')` normalisation is genuinely required
against `gold_dev`, and a clean replica leaves that path untested until the first real-data run
rediscovers the bug. It is a dimension attribute, invisible to demo output, and cheap. Recorded
in `sim_ground_truth` so it reads as intentional rather than as a generator defect.

### 7.3 `dim_date` — sufficient, but `IS_HOLIDAY` needs populating

Verified: **2023-01-01 → 2030-12-31**, 2923 rows, 9 fiscal years — fully covers the 36-month
window. `FISCAL_YEAR`/`FISCAL_QUARTER`/`IS_WEEKEND` are populated.

But `IS_HOLIDAY` is flagged on **zero rows** and `HOLIDAY_NAME` is empty. Festive-season demand
is the most realistic seasonality driver for an Indian auto manufacturer, so the generator
populates ~12–15 holidays per year (Diwali, Holi, Independence Day, regional festivals) plus
`HOLIDAY_NAME`. Data-only change to a dimension in the replica; no schema change.

### 7.4 New script with a catalog guard

**`scripts/generate_analytics_dataset.py`**, not an extension of `seed_demo_scenarios.py`. That
script's scenario constants are pinned to `gold_dev` part IDs that will not survive
regeneration, and its `--rebuild-facts` mode is already documented as destructive against
DE-shaped tables. Keeping both catalogs in one script is how the destructive mode eventually
gets pointed at the wrong catalog.

The new script **refuses to run unless the target catalog is `gold_dev_analytics`**, overridable
only by an explicit flag. `seed_demo_scenarios.py` stays as-is for `gold_dev` demo maintenance.
