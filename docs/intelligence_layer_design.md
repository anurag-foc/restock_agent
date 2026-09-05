# Intelligence layer design — the 7 nuances as detectors

Clean-slate design of the detection layer. Supersedes the signal-board-plus-eight-UC-functions
shape for anything about *how a nuance is found*. Read alongside
[dataset_generator_spec.md](dataset_generator_spec.md) (the data that exercises this — finding
ids `F1`–`F11` below refer to its §1 catalog) and
[pm_day_in_the_life.md](pm_day_in_the_life.md) (what the output is for).

---

## §0 Two changes that drive everything else

**1. Exposure is `P(stockout) × consequence`, not `shortfall_qty × unit_cost`.**
Pricing a shortage at the cost of buying the missing parts measures the wrong thing entirely —
it has no relationship to what the shortage costs you. A ₹40 fastener that halts an engine line
is not a ₹4,000 problem. And a probability makes "90%-likely ₹5L" rank above "5%-likely ₹1Cr",
which a deterministic shortfall figure cannot express at all.

**2. `action_cost` is the real cost of the real fix, not a percentage of exposure.**
The current heuristic (`exposure × 0.03` for a transfer, `× 0.15–0.50` for a buy, `× 1.00` for
nothing) is a plausible-looking number available for every candidate before any option is
chosen — which is exactly why it kept being reported to PMs as though it were a price, three
separate times, each time surviving a prose fix. Once the fix constructors (§3) compute a real
orderable quantity at a real effective unit cost, `action_cost` is an actual rupee figure a PM
can check against a quote. The failure mode disappears because the fabrication-bait is gone,
not because we asked the model more firmly.

**3. Burn is forward-looking over the replenishment horizon**, not a seasonally-corrected
historical average. The quantity that matters is demand between now and when stock lands.

**4. Every policy assumption behind a figure is disclosed with that figure** (§4, §5.1). A
number the PM cannot trace back to either measured data or a named company setting is a number
they cannot challenge — and one they cannot challenge is one they will not trust.

---

## §1 The 7 nuances are not 7 peers

This is the structural diagnosis. Treating them as a flat list of seven signals is what
collapsed the live signal set to two.

| role | nuances | what they output |
|---|---|---|
**Estimators** | 4 (seasonality), 5 (lead time) | *parameters* every other number inherits — not findings |
**Risk measure** | 3 (ranking) | `P(stockout)`, exposure, decision value |
**Consequence model** | 2 (BOM cascade) | what breaks, and what it is worth |
**Fix constructors** | 1 (transfer), 6 (supplier), 7 (MOQ) | the action, and its real cost |
**Machinery** | 8 (suppression) | a filter over findings |

Nuances 4, 5, 1, 6 and 7 can *also* raise a finding — when the estimate or the constraint is
itself the news ("this supplier's variance is unmanageable", "this MOQ forces an 8-month
overbuy"). That dual role is the thing the current design cannot express: it implements them
only as modifiers, so they can never headline, and five of the eight nuances are permanently
invisible as reasons to act.

```
ISSUE transactions ──► E1 forward burn μ_d, σ_d ──┐
delivery history  ──► E2 lead time  μ_L, σ_L ─────┼──► R  P(stockout)
on-hand + on-order ───────────────────────────────┘        │
                                                            │
production plan ──► C  parent buildable ──► consequence ────┤
                       + binding children                   ▼
                                                     exposure, decision value
transfer options ──► FX1 ┐                                  │
supplier choice  ──► FX2 ┼──► cheapest viable fix + real cost┘
orderable qty    ──► FX3 ┘                                  │
                                                            ▼
                                              S1..S8 scanners ──► findings
                                                            │
                                              nuance 8 suppression
                                                            ▼
                                                  ranked, budgeted output
```

---

## §2 Estimators

### E1 — Forward burn (nuance 4)

**In:** ISSUE rows from `fact_inventory_transaction`, `dim_date`.
**Out:** `forward_burn`, `σ_d`, `burn_confidence`, `regime`.

Order matters — the intermittency test comes **first**:

```
zero_day_fraction > 0.7  →  Croston
    burn = mean_issue_size / mean_issue_interval
    σ_d  from the size and interval distributions
otherwise  →  level × season
    level        = trailing 28d mean
    index_ahead  = mean over prior years of (consumption in the next μ_L-day
                   calendar window ÷ that year's annual mean)
    index_used   = 1 + (index_ahead − 1) × min(years_of_history / 3, 1)
    forward_burn = level × index_used / index_now
    σ_d          = residual std dev after removing level and season
```

Guards: `< 90 days` history → trailing mean only, `burn_confidence = LOW`. `< 2 years` → the
shrinkage term above (one observation per season is a coin flip dressed as a signal).

Why intermittency is checked first: most spare/MRO parts issue in lumps, and a daily mean over
85% zero days is not a small error, it is a meaningless number that then propagates into every
downstream figure. Exercised by `F9`.

**Can raise a finding** — `S6`, when a *sustained* step change (30d vs 90d vs 365d all agree)
sits against a safety stock still set for the old rate (`F7`).

### E2 — Lead time (nuance 5)

**In:** `fact_supplier_delivery` (`PLANNED_DATE_KEY` vs `DELIVERY_DATE_KEY`, and the newly added
`PART_KEY`), `dim_supplier_contract` for the contracted figure.
**Out:** `μ_L`, `σ_L`, `P90_L`, `n`, `tier`.

Hierarchical fallback, first tier that qualifies wins:

| tier | requires |
|---|---|
(supplier, part) | n ≥ 8 |
(supplier) | n ≥ 3 |
(supplier category) | n ≥ 3 |
contracted | always available, `n = 0` |

Observations are weighted by recency, exponential with a ~180-day half-life — a supplier late
last year but clean for six months is not currently drifting. `n` and `tier` travel with the
estimate always, so a report can say *how well* this is known.

**`σ_L` matters more than drift.** 40±2 days against a 40-day contract is fine; 40±14 is
unmanageable with drift exactly zero. `σ_L` is what drives the safety stock a supplier forces
you to carry, and it is what makes them expensive (§3, FX2). A mean-only check sees nothing
here. Exercised by `F4`.

**Can raise a finding** — `S5`, on drift (`μ_L − contracted` material) *or* on variance
(`σ_L / μ_L` above threshold), reported once per supplier with the affected parts attached —
never once per part, which is how one drifting supplier becomes twelve alerts.

### Note on `fact_procurement`

It carries no actual-receipt date, so it **cannot** serve E2 (see
[schema_changes_gold_dev_analytics.md](schema_changes_gold_dev_analytics.md) §1.1). Its role
here is the **inbound pipeline** only: `PENDING_QTY` + `EXPECTED_DATE_KEY` + `STATUS` give
`on_order_arriving_within_μ_L`, which R needs. Clean split, no schema change required.

---

## §3 Risk, consequence, and fixes

### R — Risk and exposure (nuance 3)

```
μ_DL      = forward_burn × μ_L
σ_DL      = √( μ_L × σ_d²  +  forward_burn² × σ_L² )
available = on_hand + on_order_arriving_within_μ_L
P_stockout = Φ( (μ_DL − available) / σ_DL )

exposure       = P_stockout × consequence
decision_value = exposure − action_cost        (action_cost from §3 FX*, a real figure)
```

`σ_DL` combines demand and lead-time uncertainty — the standard formula, and the reason both
estimators must report a spread rather than a point.

A useful property falls out: when `burn_confidence` is LOW, `σ_d` is large, so `σ_DL` is large
and `P_stockout` is pulled toward 0.5. Poorly-understood parts are automatically ranked with
less conviction instead of being assigned false certainty. That is the honest behaviour, and it
comes free rather than needing a rule.

**`consequence`** comes from C when the part feeds a planned assembly. Otherwise it falls back
to a criticality-class proxy — an acknowledged approximation (§6).

### C — Consequence: BOM cascade (nuance 2)

**In:** `fact_production_execution` (`PLANNED_QTY` by date/plant/line/model), `dim_model_bom`
(model → top-level assembly), `dim_bom` (assembly → sub-assembly → component, recursive).

Computed **at the parent, attributed to children** — never accumulated upward from each child:

```
per child c of parent p:
    buildable_from(c) = floor( available(c) / qty_per_unit(c,p) )
parent_buildable(p)   = MIN over children of buildable_from(c)     ← binding constraint
units_blocked(p)      = max( planned_qty(p, horizon) − parent_buildable(p), 0 )
value_at_risk(p)      = units_blocked(p) × unit_cost(p)            ← counted ONCE
binding_children(p)   = { c : buildable_from(c) == parent_buildable(p) }
```

The action is the **whole binding set**. If two children bind, "buy component X and the parent
is unblocked" is false — and the current implementation, which hangs each parent's full
`value_at_risk` on every child row independently, both inflates exposure and produces exactly
that false recommendation. Exercised by `F2`.

Multi-level BOMs need a recursive explosion; `available(c)` is `on_hand + on_order`, not
on-hand alone.

**Critically: a part that is both below its own safety stock and cascade-binding gets the
cascade consequence.** The current board excludes such parts from the cascade join entirely
(`WHERE on_hand >= safety_stock`), so the single most urgent case in the system — short *and*
blocking A-CRITICAL output — falls back to being priced at the cost of the parts. Exercised by
`F3`.

### FX1 — Transfer (nuance 1)

A matching problem, not a threshold. Per part, across warehouses, using each location's own
`forward_burn`:

```
needy: cover < μ_L        rich: cover > upper band, or on_hand > max
transfer_qty = min( receiver need, donor surplus above its own protected level )
benefit      = Δ(P_stockout × consequence) at receiver
               − Δ(P_stockout × consequence) at donor
action_cost  = FREIGHT_COST (now a real, populated column)
```

Rank **all** donors — the largest pile is often not the best donor, because that warehouse's
own burn may be high. Exercised by `F1`.

Also detects **pure imbalance**: nobody short yet, but the network distribution is skewed
against where consumption actually happens (`F8`'s neighbour, and the preventive case).

### FX2 — Supplier choice (nuance 6)

A 0-100 reliability score is unarguable. Convert to money:

```
effective_unit_cost = quoted_price × (1 + reject_rate)
                    + holding_rate × unit_cost × (z × σ_L × forward_burn)
```

The second term is the extra safety stock the supplier's variability forces you to hold. Now
"the cheap supplier is expensive" is arithmetic procurement can check, and it can **reverse the
price ranking** — which is the whole point. Shrink `reject_rate` toward the category mean when
`n` is small. Exercised by `F5`.

### FX3 — Orderable quantity (nuance 7)

The quantity is **computed, never chosen** — this is the rule, not a prompt instruction:

```
target_cover_days = μ_L + review_period + (z × σ_L)      ← derived, not a constant
required      = target_cover_days × forward_burn − available
orderable     = max( MOQ, ceil(required / pack_size) × pack_size )
excess        = orderable − required
excess_months = excess / forward_burn / 30
excess_cost   = excess × unit_cost × holding_rate × (excess_months / 12)
action_cost   = orderable × effective_unit_cost + excess_cost
```

`target_cover_days` is **derived per part, not set as a policy constant** (§5.1). It uses the
same `z` and `σ_L` as FX2, so the two cannot drift apart into independently-tuned knobs — and it
means an erratic supplier automatically forces more cover, which is what makes FX2's variance
premium legible rather than abstract.

Holding cost is priced over how long the excess will actually take to consume, not as a flat
percentage. When `excess_cost` is material against the exposure being avoided, **that is itself
a finding** (`S8`) — "this MOQ forces an 8-month overbuy every time; renegotiate the pack" — a
different and often more valuable action than the restock. Exercised by `F6`.

---

## §4 Scanners and the common finding shape

Each scanner runs at **its own natural grain** and emits the same row. Flattening all of them
onto one (part, warehouse) row is what made five nuances unable to headline.

| id | scanner | grain | produces |
|---|---|---|---|
S1 | stockout risk | (part, warehouse) | — |
S2 | cascade block | **(parent, warehouse)** | `F2`, `F3` |
S3 | redeployment | **(part) across network** | `F1` |
S4 | dead capital | (part, warehouse) | `F8` |
S5 | lead-time signal | **(supplier)** | `F4` |
S6 | demand shift | (part, warehouse) | `F7` |
S7 | supplier economics | (supplier, part) | `F5` |
S8 | MOQ uneconomic | (part, supplier) | `F6` |

```
finding_type · subject_type · subject_id · warehouse_id
· exposure · p_stockout · consequence
· action_type · action_detail · action_cost · decision_value
· evidence_json          -- every field the report needs, captured at detection time
· assumptions_used       -- ONLY the policy inputs this finding's figures depend on
· confidence             -- inherits burn_confidence and E2's tier/n
· suppression_key
```

### Assumption disclosure

Every policy input that shaped a number must be visible next to that number, or the PM has a
figure they cannot argue with — which by §0's own logic is a figure they cannot trust.

Three rules, and the first is what makes the others work:

1. **The scanner records which inputs it used, in `assumptions_used`.** Not the narration step,
   and not a fixed footer. A transfer recommendation spends nothing and holds nothing, so it
   discloses no holding rate; a finding with no variance-driven figure discloses no service
   level. This is structural — the narration step can only list what the scanner recorded, so
   the "optional slot gets filled anyway" failure mode has nothing to fill from.
2. **Labelled as policy, not measurement.** A PM reading `14%` needs to know it is a company
   setting someone chose, not a fact derived from their data. That distinction is the whole
   difference between an arguable number and an apparently immutable one.
3. **Stored on the finding, not looked up at render time.** If the holding rate changes next
   quarter, a quote raised this month must still show the rate its recommendation was actually
   based on. Writing the values onto each finding at detection time gets this for free.

Placement follows the existing split: the **Teams card stays a nudge** and carries no
assumptions, because nobody audits arithmetic on a phone. The **Review App detail page** carries
them, because that is where the decision is made — inline in the arithmetic trail
(`440 excess × ₹1,700 = ₹7,48,000 held for 220 days (0.60 yr) × 14%/yr = ₹63,119`) plus a
compact block naming the policy values used, so they are visible as settings rather than buried
in a computation.

Note the shape of that trail: left to right, each step checkable, **total last**. Same rule as
the existing `IF APPROVED AND WRONG` line, and for the same reason — a headline figure with the
breakdown in a parenthetical after it was wrong three times in production, because a slot for a
total that appears *before* the arithmetic gets filled from whatever number is nearest to hand.

**`evidence_json` is populated by the scanner that found the thing.** This is the property that
removes the drill-down turns from the pipeline entirely — not by optimising them, but because
there is nothing left for them to fetch. A finding is self-describing, so the narration step
cannot omit evidence, cannot call a function with a self-chosen argument, and cannot report "no
data" about a call it never made.

**Suppression (nuance 8)** then applies uniformly over findings, keyed on `suppression_key` at
the subject's own grain — so acknowledging a supplier's drift suppresses that supplier, not one
of its twelve parts. Same freshness rules as today (`REJECTED` permanent; `PENDING_APPROVAL`
2 days; `APPROVED`/`FULFILLING` lead + 3, then re-surfacing as `STALLED_COMMITMENT`), but
applied in one place over one table instead of living inside one function's `WHERE` clause.

**Ranking** (nuance 3) then takes the top findings by `decision_value` with a diversity
constraint across `finding_type` — and now has 8 real types to diversify across rather than two
wearing three names.

---

## §5 Concrete defects this fixes

Each of these was verified in the current implementation, not hypothesised:

| defect | fix |
|---|---|
Cascade exposure inflated — each of N children blocking one parent carries that parent's full `value_at_risk` | C computes at the parent, counts once, returns the binding set |
The worst case is underpriced — a part both short and cascade-binding is excluded from the cascade join (`WHERE on_hand >= safety_stock`) and priced at parts cost | C applies to it; consequence wins over shortfall |
Only 2 of 8 nuances can headline; `signal_type`'s `CASE` is mutually exclusive by construction and `STALLED_COMMITMENT` is a relabel of the other two | 8 scanners at native grain, each emitting findings |
The model picks the order quantity, which silently disables the MOQ constraint | FX3 computes it from the replenishment gap |
`action_cost` is a percentage of exposure that reads as a price | FX* produce real rupee costs |
Lead time reported per part, so one drifting supplier becomes N alerts | S5 at (supplier) grain, parts attached |
Exposure ignores probability entirely | R's `P_stockout × consequence` |

---

## §5.1 Policy inputs — decided

All three are disclosed per the rules above whenever a finding's figures depend on them.

| input | value | basis |
|---|---|---|
**`holding_rate`** | **14% / year** | bottom-up: capital 10% + warehousing 2.5% + insurance 0.5% + shrinkage/obsolescence 1%. Deliberately *not* the industry-standard 20–30%, which `market_evidence_phase1.md` §8 traces to a 1995 trade article. The 10% is a placeholder for the customer's actual WACC; the 1% obsolescence line is the softest and becomes measurable from inventory adjustments once there is history |
**`z` / service level** | **99% (2.33) A-CRITICAL · 95% (1.65) B · 90% (1.28) C** | tiered rather than blanket — `dim_part` already carries `CRITICALITY_CLASS` and `SAFETY_CRITICAL`, so treating a brake component like a trim clip is a choice, not a constraint |
**`review_period`** | **7 days** | a weekly PO cycle. The only genuine constant in FX3 — `target_cover_days` is derived around it |

**Correction this forces.** `evaluate_feasibility` currently applies a flat `0.02` to excess
value with no time dimension. At 14%/year over, say, 8 months of excess, the real figure is
~9.3% of excess value — **roughly 4–5× higher**. Every MOQ excess-holding cost shown to a PM in
every quote written to date is materially understated.

---

## §6 Approximations to keep honest

Not resolved by this design; state them wherever the numbers are shown:

1. **`consequence` for parts outside a planned assembly** — no downtime-cost or
   production-value data exists for them, so it falls back to a criticality-class proxy. This
   is the weakest number in the system and should be labelled as such rather than shown as a
   rupee figure with false precision.
2. **No `EXPOSURE_AT_DECISION`** — suppression re-surfacing stays time-based only. "Exposure
   grew 1.5× since the decision" needs the value captured at decision time, which
   `fact_restock_request` does not store.
6. **Freight cost realism** — `FREIGHT_COST` exists and will be populated by the generator, so
   FX1 can quote it. But a generated freight figure is only as defensible as the model behind
   it; against real data this becomes a genuine input, not a derivation.
