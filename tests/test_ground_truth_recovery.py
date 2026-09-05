"""Phase 2's gate: do the estimators recover the truth we planted?

This is the first thing in this project whose success is *measured* rather than reviewed. Nothing
in the existing pipeline can answer "is this number right", because there is nothing to compare
against. Here there is.

It runs locally against the same series the generator loads, so the gate is a fast test rather
than a cluster job.

**Graded against the OBSERVABLE columns, never the `TRUE_` ones.** A sparse-capture supplier's
archetype sigma is data deliberately withheld; grading against it would fail a correct estimator
for not knowing something it was never shown. `sim_ground_truth`'s docstring spells out the
distinction — this file is where getting it wrong would silently produce a meaningless pass.

Tolerances are **per regime**, because the regimes are not equally learnable. A CV-0.9 erratic
series cannot be pinned as tightly as a smooth one, and a single global tolerance would either
pass a bad estimator on smooth pairs or fail a good one on erratic pairs.
"""

from collections import Counter

import numpy as np
import pytest

from agentic_restock.estimators import burn as e1
from agentic_restock.estimators import leadtime as e2
from agentic_restock.generation import (
    contracts,
    dates,
    demand,
    entities,
    ground_truth,
)

# Median relative error the burn estimator must stay inside, per regime. Set from the noise each
# regime carries, not from what the estimator happens to score.
BURN_TOLERANCE = {
    "smooth": 0.05,
    "seasonal": 0.08,
    "trending": 0.12,
    "step": 0.15,
    "erratic": 0.25,
    "intermittent": 0.15,
}

# A regime's p90 is allowed to be worse than its median, but not unboundedly.
BURN_P90_MULTIPLE = 2.5


@pytest.fixture(scope="module")
def burn_results(histories):
    """(ground-truth row, estimate) per pair."""
    truth = {row["SUBJECT_ID"]: row for row in ground_truth.pair_rows(histories)}
    out = []
    for (part_id, warehouse_id), history in histories.items():
        estimate = e1.estimate_burn(
            history.issues,
            end_date=dates.TODAY,
            horizon_days=demand._lead_days_for(part_id),
        )
        out.append((truth[f"{part_id}@{warehouse_id}"], estimate))
    return out


@pytest.fixture(scope="module")
def leadtime_results(delivery_records):
    """(ground-truth row, estimate) per contracted (part, supplier)."""
    categories = {s["SUPPLIER_ID"]: s["SUPPLIER_TYPE"] for s in entities.suppliers()}
    # days_ago comes from the arrival day's position within the full window.
    observations = [
        e2.DeliveryObservation(
            part_id=record.event.part_id,
            supplier_id=record.event.supplier_id,
            supplier_category=categories[record.event.supplier_id],
            delay_days=float(record.delay_days),
            days_ago=float(max(0, 1100 - 1 - record.event.arrival_day)),
        )
        for record in delivery_records
    ]

    truth = {row["SUBJECT_ID"]: row for row in ground_truth.supplier_part_rows(delivery_records)}
    out = []
    for part_id, supplier_id in sorted(contracts.contracted_pairs()):
        estimate = e2.estimate_lead_time(
            observations,
            part_id=part_id,
            supplier_id=supplier_id,
            supplier_category=categories[supplier_id],
            contracted_days=contracts.contracted_lead_days(part_id, supplier_id) or 30,
        )
        out.append((truth[f"{part_id}/{supplier_id}"], estimate))
    return out


# --- E1: burn ---------------------------------------------------------------


def _burn_errors(burn_results, regime: str) -> list[float]:
    return [
        abs(estimate.level - row["RECOVERABLE_DAILY_RATE"]) / row["RECOVERABLE_DAILY_RATE"]
        for row, estimate in burn_results
        if row["REGIME"] == regime and row["RECOVERABLE_DAILY_RATE"] > 0.01
    ]


@pytest.mark.parametrize("regime", sorted(BURN_TOLERANCE))
def test_burn_recovers_the_current_rate(burn_results, regime):
    errors = _burn_errors(burn_results, regime)
    assert len(errors) >= 3, f"{regime}: only {len(errors)} gradeable pairs"
    median = float(np.median(errors))
    assert median <= BURN_TOLERANCE[regime], f"{regime}: median error {median:.1%}"


@pytest.mark.parametrize("regime", sorted(BURN_TOLERANCE))
def test_burn_error_does_not_have_a_long_tail(burn_results, regime):
    """A good median hides nothing if the p90 is wild — that is where a PM meets a wrong number."""
    errors = _burn_errors(burn_results, regime)
    p90 = float(np.percentile(errors, 90))
    assert p90 <= BURN_TOLERANCE[regime] * BURN_P90_MULTIPLE, f"{regime}: p90 {p90:.1%}"


def test_intermittent_pairs_route_to_croston(burn_results):
    """Ordering matters: above ~70% zero days a daily mean is meaningless, not just imprecise.

    STOPPED counts: it is a Croston sub-case for a part that has been silent for several
    multiples of its own demand interval, and reporting its historical rate would hide dead
    capital behind a healthy-looking burn.
    """
    for row, estimate in burn_results:
        if row["REGIME"] == "intermittent":
            assert estimate.method in (e1.METHOD_CROSTON, e1.METHOD_STOPPED), row["SUBJECT_ID"]
            if row["QUIET_SINCE_DATE_KEY"] is None:
                assert estimate.method == e1.METHOD_CROSTON, row["SUBJECT_ID"]


def test_dense_series_never_route_to_croston(burn_results):
    for row, estimate in burn_results:
        if row["REALISED_ZERO_DAY_FRACTION"] <= e1.INTERMITTENCY_THRESHOLD:
            assert estimate.method != e1.METHOD_CROSTON, row["SUBJECT_ID"]


def test_short_history_is_reported_as_low_confidence(burn_results):
    """The risk model widens sigma_DL on LOW confidence, which is how a poorly-understood part
    gets ranked with less conviction instead of false certainty."""
    for row, estimate in burn_results:
        if row["HISTORY_COHORT"] == "sparse":
            assert estimate.confidence == e1.CONFIDENCE_LOW, row["SUBJECT_ID"]


def test_stopped_demand_reads_as_zero(burn_results):
    """Dead-capital pairs: the current rate genuinely is nothing, and cover is unbounded."""
    graded = [
        (row, est) for row, est in burn_results if row["QUIET_SINCE_DATE_KEY"] is not None
    ]
    assert graded
    for row, estimate in graded:
        assert estimate.level == pytest.approx(0.0, abs=0.01), row["SUBJECT_ID"]


def test_seasonal_pairs_get_a_forward_burn_that_differs_from_level(burn_results):
    """If forward_burn always equalled level, the seasonal correction would be doing nothing."""
    seasonal = [
        (row, est)
        for row, est in burn_results
        if row["REGIME"] == "seasonal" and est.method == e1.METHOD_SEASONAL
    ]
    assert seasonal
    differing = [
        1 for _row, est in seasonal if abs(est.forward_burn - est.level) / max(est.level, 1e-9) > 0.02
    ]
    assert len(differing) >= len(seasonal) * 0.5


def test_flat_pairs_are_not_given_a_spurious_seasonal_correction(burn_results):
    smooth = [
        est
        for row, est in burn_results
        if row["REGIME"] == "smooth" and est.method == e1.METHOD_SEASONAL
    ]
    assert smooth
    indices = [est.seasonal_index_ahead for est in smooth]
    assert float(np.median(indices)) == pytest.approx(1.0, abs=0.15)


# --- E2: lead time ----------------------------------------------------------


def test_leadtime_picks_the_tier_the_data_supports(leadtime_results):
    """The ladder does not degrade monotonically with the pair count — the supplier pool spans
    that supplier's other parts. Predicting the tier from the pair count alone disagreed on 28
    of 62 pairs."""
    mismatches = [
        (row["SUBJECT_ID"], row["EXPECTED_FALLBACK_TIER"], estimate.tier)
        for row, estimate in leadtime_results
        if row["EXPECTED_FALLBACK_TIER"] != estimate.tier
    ]
    assert not mismatches, mismatches[:5]


def test_leadtime_recovers_the_observed_spread(leadtime_results):
    """Graded on the unweighted sigma, which is what the ground-truth figure measures. The
    weighted one production uses cannot be compared like-for-like against a statistic with no
    notion of recency."""
    graded = [
        (row, est)
        for row, est in leadtime_results
        if est.tier == e2.TIER_PAIR and (row["OBSERVED_SIGMA_DELAY_DAYS"] or 0) > 0
    ]
    assert len(graded) >= 5
    for row, estimate in graded:
        assert estimate.sigma_unweighted == pytest.approx(
            row["OBSERVED_SIGMA_DELAY_DAYS"], rel=0.02
        ), row["SUBJECT_ID"]


def test_leadtime_recovers_the_observed_mean_delay(leadtime_results):
    graded = [
        (row, est)
        for row, est in leadtime_results
        if est.tier == e2.TIER_PAIR and row["OBSERVED_MEAN_DELAY_DAYS"] is not None
    ]
    assert len(graded) >= 5
    for row, estimate in graded:
        # Recency-weighted, so it legitimately differs from the flat mean — but not wildly, and
        # the direction must agree.
        assert abs(estimate.mean_delay - row["OBSERVED_MEAN_DELAY_DAYS"]) < 4.0, row["SUBJECT_ID"]


def test_cold_start_falls_back_to_the_contract_without_inventing_spread():
    """Tested directly, because this dataset legitimately never reaches it.

    With 744 captured deliveries across two supplier categories there is always *some* pool to
    estimate from, and using it is the correct behaviour — falling through to the contract when
    data exists would be worse. So the contracted tier is a genuine cold-start path rather than
    dead code, and asserting the dataset produces it would mean degrading the dataset to suit
    the test. An earlier version did assert that and passed vacuously on an empty list.
    """
    estimate = e2.estimate_lead_time(
        [],
        part_id="P0001",
        supplier_id="SUP001",
        supplier_category="TIER_1",
        contracted_days=40,
    )
    assert estimate.tier == e2.TIER_CONTRACTED
    assert estimate.mu_lead == 40.0
    assert estimate.sigma_lead == 0.0  # no spread invented where none was measured
    assert estimate.p90_lead == 40.0
    assert estimate.observations == 0
    assert not estimate.is_measured


def test_variance_and_drift_are_independently_recovered(leadtime_results):
    """F4's premise: on contract on average, unmanageable in practice. An estimator that only
    tracked the mean would rate the erratic supplier as fine."""
    by_archetype: dict[str, list] = {}
    for row, estimate in leadtime_results:
        if estimate.tier == e2.TIER_PAIR:
            by_archetype.setdefault(row["SUPPLIER_ARCHETYPE"], []).append(estimate)

    erratic = by_archetype.get("loose") or []
    drifting = by_archetype.get("drifting") or []
    assert erratic and drifting

    # Erratic: near-zero drift, wide spread.
    assert abs(float(np.mean([e.drift_days for e in erratic]))) < 4
    assert float(np.mean([e.sigma_lead for e in erratic])) > 8
    # Drifting: genuinely late, but predictably so.
    assert float(np.mean([e.drift_days for e in drifting])) > 4
    assert float(np.mean([e.sigma_lead for e in drifting])) < 6


def test_every_data_backed_fallback_tier_is_exercised(leadtime_results):
    """The three tiers that fit from data, or the ladder is untested however well it scores.

    `contracted` is deliberately excluded — see `test_cold_start_falls_back_to_the_contract`.
    """
    tiers = Counter(est.tier for _row, est in leadtime_results)
    for tier in (e2.TIER_PAIR, e2.TIER_SUPPLIER, e2.TIER_CATEGORY):
        assert tiers[tier] >= 3, tiers
