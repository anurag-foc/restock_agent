# Simulation feature — design

**Status:** built. `src/agentic_restock/simulation/`, `notebooks/simulation/run_simulation.py`,
`resources/jobs/simulation_job.yml`, and the app's Simulation page. This doc records the design
*as built*; where the build changed a decision, §2.2 and §8 say so. §5 and §6 describe the
*intended* table and page shape and were not updated as those were built — read `persistence.py`
and `SimulationPage.tsx` for what is actually there. §9 is current. Companion to
[intelligence_layer_design.md](intelligence_layer_design.md) (what the detectors compute) and
[dataset_generator_spec.md](dataset_generator_spec.md) (the dataset they are measured against).

## 1. The ask, and what it actually resolves to

> On the Admin can we create a Simulation feature where different demand patterns for the
> inventory can be tried out and the resulting cost savings or losses can be presented in a
> quantified manner as a chart or something. So that each simulation run contributes to a
> measurable performance indicator for the tool we have developed. It can also be used to
> compare the different forecasting methods and their effectiveness.

Three requirements are stacked in that paragraph, and they are not equally hard:

| | requirement | status |
|---|---|---|
| R1 | Try out different demand patterns | the generator already parameterises these |
| R2 | Quantify the result in money | **the new work** — needs a scoring rule that does not exist yet |
| R3 | Compare forecasting methods | falls out of R2 once `consumption_model` is a run input |

R2 is the whole feature. R1 and R3 are plumbing around it.

**The reason this is tractable at all is that the ground truth already exists.** Every simulation
needs an answer to "what *should* have happened", and inventing one is normally the expensive,
arguable part. This repo built it for the Phase 2 gate:

- [`generation/demand.py`](../src/agentic_restock/generation/demand.py) — `pair_params()` carries
  the true generative parameters for every pair, which is what makes a forward counterfactual
  computable at all (see §2.2).
- [`generation/scenarios.py`](../src/agentic_restock/generation/scenarios.py) —
  `findings_for(part, warehouse)` returns the findings planted on a pair. Kept as context on the
  detail rows; **not** the scoring label — §2.2 explains why.
- [`generation/ground_truth.py`](../src/agentic_restock/generation/ground_truth.py) —
  `sim_ground_truth` records both the parameter we generated with and the parameter an estimator
  could *observably* recover. The distinction is load-bearing: grading against the former fails a
  correct estimator.
- [`tests/test_ground_truth_recovery.py`](../tests/test_ground_truth_recovery.py) — already grades
  burn accuracy per regime, routing, and confidence reporting.

So this feature is not "build a simulation". It is **take the gate that already runs in pytest,
price its outcomes in rupees, and put a page on it.**

## 2. The KPI

Forecast accuracy is the wrong axis. The ask says *cost savings or losses*, and MASE is not
money — the translation from "the burn estimate was 12% high" to "that would have cost ₹4.2 lakh"
runs entirely through this system's own pricing: `exposure` from
[`estimators/risk.py`](../src/agentic_restock/estimators/risk.py), `action_cost` from
[`detectors/fixes.py`](../src/agentic_restock/detectors/fixes.py).

A simulation run produces a confusion matrix over pairs, priced:

| | pair will really run short | pair is really fine |
|---|---|---|
| **pipeline raised it** | `+ decision_value` — value delivered | `− action_cost` — false alarm: stock bought that was not needed |
| **pipeline stayed quiet** | `− exposure` — the miss | `₹0` — correctly quiet |

```
net_value = Σ decision_value(caught)
          − Σ exposure(missed)
          − Σ action_cost(false_alarms)
```

Every term is already computed by the pipeline; nothing new is invented to price a cell.

Notes on the cells, in the order they will be argued about:

- **`decision_value`, not `exposure`, for a catch.** `exposure − action_cost` is what the finding
  is worth net of fixing it. Crediting full exposure would make an expensive fix look as good as
  a cheap one, which is the mistake the redesign already corrected once in the ranking.
- **The miss is the expensive cell, and that is correct.** A pair that genuinely needed action and
  got none realises its exposure. This is what should dominate the metric — a tool that is quiet
  and wrong is worse than one that is noisy and right.
- **The false alarm costs `action_cost`, not exposure.** A PM acting on a finding that was not
  real spends the fix cost. They do not lose the exposure, because there was none. This cell is the
  alert-fatigue claim with a number attached, and it is the only cell that punishes a loose
  detector.
- **The output budget is inside the loop, not outside it.** Selection caps at 4 findings with a
  max of 2 per type. A pair that was correctly detected but *not selected* is scored as a **miss**,
  not a catch. This is deliberate: the budget is a product decision and it should have to pay for
  itself in the metric like everything else.

  The build takes this one step further: **every run is scored twice**, once on what the detectors
  found and once on what survived the budget. The difference is reported as `BUDGET_COST`, in
  rupees. Scoring only what the PM saw would blame the detectors for the budget; scoring only what
  was detected would hide the budget's cost entirely.

### 2.1 What truth is — changed during the build

The first implementation scored against `scenarios.expect_finding()`. That was wrong, and the
way it was wrong is worth keeping: that function answers *"was a problem deliberately planted
here"*, which labels ~33 of 278 pairs and treats every other pair as one that should stay quiet.
But background pairs are drawn from the same random regimes as the planted ones and can be
genuinely short. Real catches were scored as false alarms, and the first run reported **a recall
of exactly zero** — a plausible-looking number from a broken rule, which is this project's
recurring failure shape.

Truth is now **realised shortage**, in [`simulation/truth.py`](../src/agentic_restock/simulation/truth.py):
will this pair consume more than it holds before a replenishment can land, computed from the
parameters the generator drew with? It covers every pair, it is what the product actually claims
to predict, and it is the only basis on which "cost savings or losses" means anything.

Expected forward demand, not a fresh random draw — a noisy future would add a second source of
randomness between two engines being compared, and a difference in net value could then be a
difference in the dice.

**Truth decides the cell; the product's own pricing decides the amount.** Whether a pair needed
action comes from the generator. What that outcome is worth comes from `risk.py` and `fixes.py`.
Letting the pipeline supply both would make the score unfalsifiable.

### 2.2 What the number is not

It is a **simulated** figure measured against a demand model we wrote. It is not measured savings,
and it never becomes measured savings by being charted.

This matters more here than it would elsewhere. This project has a recorded history of
plausible-looking numbers being presented as measured (see the fabrication incidents in
[redesign_tracker.md](redesign_tracker.md)), and `risk.py` already carries the discipline in code —
`CONSEQUENCE_MULTIPLE_BY_CLASS` is commented as **"THE WEAKEST NUMBER IN THE SYSTEM"** and must be
"labelled as an estimate wherever it is shown, never dressed as a measured figure".

A chart reading "₹4.2 cr saved" will be screenshotted into a deck within a week. So the constraint
is structural, not editorial:

- Every surface showing `net_value` shows the seed and regime mix beside it.
- The label is **"simulated on this demand pattern"**, never "saved".
- Cross-engine *deltas* are the honest headline; the absolute figure is scenario-dependent and
  should be presented as secondary.

## 3. What a run takes as input

| input | source | why it is a knob |
|---|---|---|
| regime mix | [`generation/demand.py`](../src/agentic_restock/generation/demand.py) — `smooth`, `seasonal`, `erratic`, `step` | R1, directly. The current generator targets a fixed mix; a run overrides it |
| seed | `generation/demand.py::_seed` | reproducibility. Two engines must be compared on the *same* drawn world or the comparison is noise |
| `consumption_model` | [`settings.py`](../src/agentic_restock/settings.py) | R3 |
| other settings | `settings.py` | holding rate, service level etc. — a run is scored under one policy |

The regime mix is the demand-pattern control the ask asks for. `PairParams` already carries
`level`, `seasonal_amplitude`, `step_day` and `step_factor`, so the shapes are parameterised; the
feature exposes the mix, not new maths.

**A comparison run holds everything constant except the one variable.** Comparing engines means
the same seed and the same regime mix, `consumption_model` alone differing. The API should make
that the easy path — a "compare" run takes a list of engines and fans out internally rather than
asking the operator to keep two independent runs aligned by hand.

## 4. Where it runs

The pipeline is pure pandas/numpy with Spark only at the read/write edges. That is what makes this
cheap, and it has one important consequence:

**A simulation run writes no fact table and needs no scratch catalog.** Generate → estimate → scan
→ select → score happens entirely in memory. `gold_dev_analytics` is never touched, so a run
cannot corrupt the data the live pipeline reads. Only the result row is persisted.

It is too heavy for the Node server (~295K generated rows per run), so it follows the pattern the
app already uses for `restock_decision`:

```
SimulationPage  → POST /api/simulations          (validate, trigger, return run_id)
                     → triggers `simulation` job with a run spec
                         → generate → estimate → scan → select → score
                         → write one sim_run row (+ sim_run_pair detail)
                → GET /api/simulations           (list, poll status)
                → GET /api/simulations/:runId    (result + per-cell breakdown)
```

The app does not compute and does not write the result — same split as
`POST /api/quotes/:quoteId/decisions`, and for the same reason.

`sim_events` and `sim_pair_scenarios` are named in `config.py` and currently empty, left over from
a retired generator that used them for backtest attribution. They are the natural home for
`sim_run` / `sim_run_pair`; reuse or rename them rather than adding a fourth empty table.

## 5. Tables

**`sim_run`** — one row per run.

| column | note |
|---|---|
| `RUN_ID` | deterministic from (seed, regime mix, settings hash) so an identical re-run is idempotent |
| `RUN_TS`, `TRIGGERED_BY` | |
| `SEED`, `REGIME_MIX` | the drawn world. Without these the number is unreproducible and therefore unciteable |
| `CONSUMPTION_MODEL` | the engine under test |
| `SETTINGS_JSON` | full policy snapshot — a `net_value` computed under a different holding rate is a different number |
| `PAIRS_TOTAL`, `PAIRS_EXPECTED`, `PAIRS_QUIET` | denominators |
| `CAUGHT`, `MISSED`, `FALSE_ALARMS`, `CORRECTLY_QUIET` | counts |
| `VALUE_DELIVERED`, `VALUE_MISSED`, `VALUE_WASTED`, `NET_VALUE` | rupees |
| `BURN_MEDIAN_ERROR`, `BURN_P90_ERROR` | the accuracy leg, kept as diagnosis. Explains *why* an engine scored as it did; never the headline |
| `STATUS`, `ERROR` | a failed run must be visible, same argument as `scan_run_log` |

**`sim_run_pair`** — one row per (run, part, warehouse): planted findings, findings produced,
selected or not, and the priced cell. This is what makes a bad `net_value` diagnosable instead of
merely reportable; without it the feature produces a number nobody can argue with or learn from.

## 6. UI

One page, three regions. Chart selection and the accompanying notation are deliberately left to
implementation — the point to settle here is what gets plotted, not how.

1. **Set up a run** — regime mix, seed, engine(s). Defaults reproduce the shipped dataset so the
   first run is meaningful with no configuration.
2. **Result** — `net_value` with the four cells broken out. The four-cell breakdown is not optional:
   a single net figure hides whether the pipeline scored well by catching everything or by
   flagging nothing.
3. **Compare** — engines side by side on the same seed and mix, with the **delta** as the headline.

## 6a. Not built yet

**The regime mix is not a run input.** `generation/demand.py`'s `_build_regime_cycle()` is a fixed
30/25/10/8/15/12 split, so "try out different demand patterns" currently means the one shipped
pattern. The live knobs are engines, budget and label. Parameterising the mix means threading an
override through `pair_params()` and clearing the generator's caches — contained, but it touches
the module every other gate depends on, so it was left until the scoring it feeds was settled.

## 7. Out of scope

- **Anything claiming production savings.** This scores the pipeline against a generated world.
  Attribution against real outcomes needs the decision history in `fact_restock_request` and is a
  different feature.
- **Tuning.** A run reports; it does not write settings back. An automatic "pick the best engine"
  loop over a synthetic dataset optimises for the generator, not for the client's inventory.
- **New demand mathematics.** If a regime is missing, it is added to `generation/demand.py` with
  its own assertion, not special-cased in the simulation.

## 8. Open questions

1. **Is `expect_finding()`'s planted-findings ground truth rich enough?** It is currently keyed to
   the ~11 hand-placed F-scenarios. Scoring the *whole* pair universe needs a truth rule for
   background pairs too, rather than treating every unplanted pair as "should stay quiet" — which
   is close to true by construction but not asserted anywhere.
2. **How is a cascade scored?** A cascade finding subsumes its binding children. Crediting the
   parent and the children separately would double-count exactly the way the superseded design
   double-counted cascade value.
3. **Should the budget be a run input?** It would make "what does a budget of 4 cost us in missed
   value?" answerable, which is the sharpest evidence for the product thesis — and also the
   sharpest evidence against it if the answer is bad.

## 9. The bug this feature surfaced

Worth recording because it is the same shape as everything else in `redesign_tracker.md`:
plausible output, no error, and it made the pipeline look *worse* than it is.

`simulation/frames.py` claims in its own docstring to mirror `jobs/positions.py`'s queries. Its
in-transit adapter did not. It summed `PENDING_QTY` by `PART_KEY` alone and applied no status
filter, where the query groups by `(PART_KEY, WAREHOUSE_KEY)` and filters
`STATUS IN ('ISSUED','PARTIAL')`. Every warehouse holding a part was therefore credited with the
whole network's open orders — mean in-transit 9,901 against mean on-hand 3,582. Available stock
followed, days of cover followed, and S1, whose entire test is whether cover outlasts the lead
time, went quiet on pairs that were genuinely short.

Fixing it exposed a second one underneath, in the data rather than the adapter.
`fact_procurement.WAREHOUSE_KEY` exists and has since 2026-09-05
([schema_changes §1.3](schema_changes_gold_dev_analytics.md)), but it was populated by a one-off
backfill through `PLANT_KEY` — and `generation/procurement.py` assigned the *default* plant to any
warehouse with no plant link, which is every regional DC. So 528 DC-bound orders were recorded
against WH001. No DC pair had any inbound stock at all; one plant store carried the network's, and
the backfill's "1119/1119 rows, 0 unresolved" measured that every row resolved rather than that any
resolved correctly.

`generation/procurement.py` now emits the destination it always knew, so the column is populated at
load time instead of by a statement outside the loader — which also means the next
`--prune --load` no longer resets it to NULL. **This affects the production pipeline, not only the
simulation**: `refresh_positions` reads availability from the same column.

Gated by `tests/test_simulation_inbound.py`: inbound summed across warehouses must equal the
pending quantity on the part's open POs, every warehouse must have some inbound pipeline, and
network in-transit must not exceed network on-hand.
