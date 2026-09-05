"""`sim_ground_truth` — the parameters we generated with.

This is the table that makes detector quality *measurable* instead of reviewable. Nothing in the
current pipeline can answer "is this number right", because there is nothing to compare against.
With this, Phase 2 can ask concretely: does the burn estimator recover the level we drew from,
does the lead-time estimator recover the spread, and how many pairs we planted as *quiet*
produced a finding anyway. That last figure is the alert-fatigue claim, quantified.

**Both the true parameter and the observable one are recorded, deliberately.** They differ, and
the difference is not an error:

- A step-change pair's `true_level` is its pre-step rate; what an estimator should recover today
  is `true_effective_level`. Comparing against the former would fail a correct estimator.
- A sparse-capture supplier's `true_sigma_lead` comes from its archetype, but only
  `observed_sigma_delay` is visible in the two deliveries that were recorded. Grading the
  estimator against the archetype would penalise it for data we withheld on purpose.

So a gate compares against the *observable* column and uses the true column to check that the
generator itself did what it claimed. Conflating the two is how a dataset silently grades
detectors on information they never had.
"""

from __future__ import annotations

import numpy as np

from agentic_restock.generation import (
    contracts,
    dates,
    entities,
    scenarios,
    supplier_facts,
)
from agentic_restock.generation.replenishment import PairHistory
from agentic_restock.generation.supplier_facts import DeliveryRecord

SUBJECT_PAIR = "PAIR"
SUBJECT_SUPPLIER_PART = "SUPPLIER_PART"


def _current_rate(history: PairHistory) -> float:
    """The deseasonalised daily rate in force TODAY — the burn estimator's gate target.

    Deliberately *deseasonalised*: it grades the estimator's `level`, which is also
    deseasonalised. The seasonal factor is what `forward_burn` adds back for the horizon, and a
    full-window mean over 3 years averages the seasonal cycle out on its own.

    **Measured off the realised series, not read from the parameters.** The distinction matters:
    a Croston process drawn with mean interval 12 and mean size 4 does not realise a rate of
    exactly 4/12 over ~90 events, and an estimator can only recover what the sample shows.
    Grading against the parameter ratio scored a correct estimator at 9% error where the sample
    truth put it at 0.7%.

    Three corrections, each for a real property of how the series was built:
      - quiet:  zero, for pairs whose demand stopped — their current rate genuinely is nothing
      - step:   measured only after the change, so the old rate is not blended in
      - trend:  the segment mean sits at the segment's MIDPOINT, so it is scaled forward to
                today. Nothing else is non-stationary, and without this a trending pair's target
                lags reality by ~14%

    Weekends need no adjustment: they are already zeroed in the series being measured.
    """
    params = history.params
    issues = history.issues.astype(float)

    if params.quiet_since_day is not None:
        return 0.0

    start = params.step_day if params.step_day is not None else 0
    segment = issues[start:]
    if not len(segment):
        return 0.0

    rate = float(segment.mean())

    if params.trend_per_day:
        midpoint = start + (len(segment) - 1) / 2.0
        end = params.history_days - 1
        at_midpoint = 1.0 + params.trend_per_day * midpoint
        at_end = 1.0 + params.trend_per_day * end
        if at_midpoint > 0:
            rate *= at_end / at_midpoint

    return rate


def pair_rows(histories: dict[tuple[str, str], PairHistory]) -> list[dict]:
    """One row per (part, warehouse): the demand process and what it realised."""
    rows: list[dict] = []

    for (part_id, warehouse_id), history in sorted(histories.items()):
        params = history.params
        issues = history.issues.astype(float)
        nonzero = issues[issues > 0]
        n = len(issues)

        rows.append(
            {
                "SUBJECT_TYPE": SUBJECT_PAIR,
                "SUBJECT_ID": f"{part_id}@{warehouse_id}",
                "PART_ID": part_id,
                "WAREHOUSE_ID": warehouse_id,
                "SUPPLIER_ID": contracts.preferred_supplier(part_id),
                # --- the generative model ---------------------------------
                "REGIME": params.regime,
                "HISTORY_DAYS": params.history_days,
                "HISTORY_COHORT": params.history_cohort,
                "TRUE_LEVEL": round(params.level, 4),
                # What an estimator should recover TODAY -- after any step change.
                "TRUE_EFFECTIVE_LEVEL": round(params.effective_level, 4),
                "TRUE_NOISE_CV": round(params.noise_cv, 4),
                "TRUE_SEASONAL_AMPLITUDE": round(params.seasonal_amplitude, 4),
                "TRUE_TREND_PER_DAY": round(params.trend_per_day, 6),
                "STEP_DATE_KEY": (
                    dates.day_index_key(params.step_day, n)
                    if params.step_day is not None
                    else None
                ),
                "TRUE_STEP_FACTOR": round(params.step_factor, 4),
                "TRUE_MEAN_INTERVAL_DAYS": round(params.mean_interval_days, 4),
                "TRUE_MEAN_ISSUE_SIZE": round(params.mean_issue_size, 4),
                "QUIET_SINCE_DATE_KEY": (
                    dates.day_index_key(params.quiet_since_day, n)
                    if params.quiet_since_day is not None
                    else None
                ),
                # --- what the series actually shows -----------------------
                #
                # The burn estimator's gate target -- see `_current_rate`. NOT
                # `TRUE_EFFECTIVE_LEVEL`: plant stores have weekends zeroed *after* the level is
                # applied so their true daily rate is ~5/7 of it, and a step pair's full-window
                # mean blends the old and new rates.
                "RECOVERABLE_DAILY_RATE": round(_current_rate(history), 4),
                "REALISED_ISSUE_MEAN": round(float(issues.mean()), 4),
                "REALISED_NONZERO_MEAN": (
                    round(float(nonzero.mean()), 4) if len(nonzero) else 0.0
                ),
                "REALISED_ISSUE_CV": (
                    round(float(nonzero.std() / nonzero.mean()), 4)
                    if len(nonzero) > 1 and nonzero.mean() > 0
                    else 0.0
                ),
                "REALISED_ZERO_DAY_FRACTION": round(float((issues == 0).mean()), 4),
                # --- stock position today ----------------------------------
                "SAFETY_STOCK_QTY": params.safety_stock,
                "MAX_STOCK_LEVEL": params.max_stock,
                "CLOSING_QTY": history.closing_qty,
                "IN_TRANSIT_QTY": history.in_transit_qty,
                "UNIT_COST": params.unit_cost,
                "CRITICALITY_CLASS": params.criticality_class,
                # --- what the detectors are expected to do -----------------
                "PLANTED_FINDING_IDS": ",".join(params.finding_ids) or None,
                "EXPECT_FINDING": scenarios.expect_finding(part_id, warehouse_id),
                "DW_SOURCE": entities.DW_SOURCE,
            }
        )

    return rows


def supplier_part_rows(records: list[DeliveryRecord]) -> list[dict]:
    """One row per contracted (part, supplier): true lead behaviour and what was captured.

    Includes pairs with **zero** captured deliveries — those are the contracted-only tier of the
    fallback ladder, and a ground-truth table that omitted them would make the tier invisible to
    any gate checking coverage.
    """
    summary = supplier_facts.observed_lead_summary(records)
    tiers = supplier_facts.capture_tiers()

    # Pool sizes at each level of E2's ladder. The supplier pool spans that supplier's other
    # parts, which is why the pair count alone cannot predict the tier.
    categories = {s["SUPPLIER_ID"]: s["SUPPLIER_TYPE"] for s in entities.suppliers()}
    supplier_n: dict[str, int] = {}
    category_n: dict[str, int] = {}
    for record in records:
        sup = record.event.supplier_id
        supplier_n[sup] = supplier_n.get(sup, 0) + 1
        cat = categories[sup]
        category_n[cat] = category_n.get(cat, 0) + 1

    rows: list[dict] = []

    for part_id, supplier_id in sorted(contracts.contracted_pairs()):
        contracted = contracts.contracted_lead_days(part_id, supplier_id) or 0
        true_mu, true_sigma = contracts.true_lead_parameters(supplier_id, contracted)
        observed = summary.get((part_id, supplier_id))
        contract = contracts.contract_for(part_id, supplier_id)

        rows.append(
            {
                "SUBJECT_TYPE": SUBJECT_SUPPLIER_PART,
                "SUBJECT_ID": f"{part_id}/{supplier_id}",
                "PART_ID": part_id,
                "WAREHOUSE_ID": None,
                "SUPPLIER_ID": supplier_id,
                "SUPPLIER_ARCHETYPE": contracts.archetype_of(supplier_id),
                "CONTRACTED_LEAD_DAYS": contracted,
                "TRUE_MU_LEAD_DAYS": round(true_mu, 4),
                "TRUE_SIGMA_LEAD_DAYS": round(true_sigma, 4),
                "TRUE_REJECT_RATE": round(contracts.reject_rate(supplier_id), 5),
                "CAPTURE_TIER": tiers.get((part_id, supplier_id), "none"),
                # Observable from the captured sample only -- what a gate should grade against.
                "OBSERVED_DELIVERY_N": observed["n"] if observed else 0,
                "OBSERVED_MEAN_DELAY_DAYS": (
                    round(observed["observed_mean_delay"], 4) if observed else None
                ),
                "OBSERVED_SIGMA_DELAY_DAYS": (
                    round(observed["observed_sigma_delay"], 4) if observed else None
                ),
                "OBSERVED_OTD_RATE": (
                    round(observed["otd_rate"], 4) if observed else None
                ),
                "OBSERVED_SUPPLIER_N": supplier_n.get(supplier_id, 0),
                "OBSERVED_CATEGORY_N": category_n.get(categories[supplier_id], 0),
                "EXPECTED_FALLBACK_TIER": _expected_fallback_tier(
                    observed["n"] if observed else 0,
                    supplier_n.get(supplier_id, 0),
                    category_n.get(categories[supplier_id], 0),
                ),
                "MOQ": contract["moq"] if contract else None,
                "PACK_SIZE": contract["pack_size"] if contract else None,
                "CONTRACT_UNIT_COST": contract["unit_cost"] if contract else None,
                "IS_PREFERRED": contract["is_preferred"] if contract else None,
                "DW_SOURCE": entities.DW_SOURCE,
            }
        )

    return rows


def _expected_fallback_tier(pair_n: int, supplier_n: int, category_n: int) -> str:
    """Which tier of E2's ladder the captured data supports for this pair.

    Recorded rather than left implicit so the Phase 2 gate can assert the estimator picked the
    tier the data supports, not merely that its number was close.

    **All three pool sizes matter.** An earlier version keyed off the pair count alone and
    disagreed with the estimator on 28 of 62 pairs — because the supplier pool aggregates across
    that supplier's other parts and is frequently rich even where a single pair is sparse. The
    ladder does not degrade monotonically with the pair count, so predicting it from that count
    was simply wrong.

    Thresholds are imported from the estimator rather than restated. The estimator is the
    authority on its own ladder, and two copies of `8 / 3 / 3` would drift apart.
    """
    from agentic_restock.estimators import leadtime

    if pair_n >= leadtime.MIN_OBSERVATIONS_PAIR:
        return leadtime.TIER_PAIR
    if supplier_n >= leadtime.MIN_OBSERVATIONS_SUPPLIER:
        return leadtime.TIER_SUPPLIER
    if category_n >= leadtime.MIN_OBSERVATIONS_CATEGORY:
        return leadtime.TIER_CATEGORY
    return leadtime.TIER_CONTRACTED


def all_rows(
    histories: dict[tuple[str, str], PairHistory], records: list[DeliveryRecord]
) -> list[dict]:
    return pair_rows(histories) + supplier_part_rows(records)


# ---------------------------------------------------------------------------
# Coverage summary -- what Phase 2's gate will assert against
# ---------------------------------------------------------------------------


def coverage(
    histories: dict[tuple[str, str], PairHistory], records: list[DeliveryRecord]
) -> dict:
    """A compact digest of what the dataset contains, for the generator's own assertions."""
    pairs = pair_rows(histories)
    supplier_parts = supplier_part_rows(records)

    expecting = [row for row in pairs if row["EXPECT_FINDING"]]
    planted = {
        finding
        for row in pairs
        if row["PLANTED_FINDING_IDS"]
        for finding in row["PLANTED_FINDING_IDS"].split(",")
    }

    regimes: dict[str, int] = {}
    cohorts: dict[str, int] = {}
    for row in pairs:
        regimes[row["REGIME"]] = regimes.get(row["REGIME"], 0) + 1
        cohorts[row["HISTORY_COHORT"]] = cohorts.get(row["HISTORY_COHORT"], 0) + 1

    tiers: dict[str, int] = {}
    for row in supplier_parts:
        tier = row["EXPECTED_FALLBACK_TIER"]
        tiers[tier] = tiers.get(tier, 0) + 1

    return {
        "pairs": len(pairs),
        "pairs_expecting_a_finding": len(expecting),
        "quiet_fraction": round(1 - len(expecting) / len(pairs), 4),
        "planted_findings": sorted(planted),
        "regimes": regimes,
        "history_cohorts": cohorts,
        "lead_time_fallback_tiers": tiers,
        "supplier_part_subjects": len(supplier_parts),
        "median_realised_burn": round(
            float(np.median([row["REALISED_ISSUE_MEAN"] for row in pairs])), 2
        ),
    }
