"""What was *actually* going to happen — the counterfactual a run is scored against.

The first attempt at this used `scenarios.expect_finding()`, and it was wrong in a way worth
recording. That function answers "was a problem deliberately planted on this pair", which is
the right question for the detector gate (`tests/test_ground_truth_recovery.py`) and the wrong
one here. It labels ~33 of 278 pairs and treats every other pair as one that should stay quiet
— but background pairs are drawn from the same random regimes as the planted ones and can be
genuinely short. Scoring against it reported real catches as false alarms and produced a recall
of zero.

So truth here is **realised shortage**, computed from the parameters the generator drew with:
will this pair run out before a replenishment can land? That covers every pair, it is what the
product actually claims to predict, and it is the only basis on which "cost savings or losses"
means anything.

**Expected forward demand, not a fresh random draw.** The pair's true level is re-seasonalised
over its own replenishment window and compared against stock on hand plus in transit. Drawing a
noisy future instead would add a second source of randomness between the two engines being
compared, and a difference in net value could then be a difference in the dice.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agentic_restock.generation import dates, demand, policy


@dataclass(frozen=True)
class PairTruth:
    """What the generator knows about one pair's near future."""

    part_id: str
    warehouse_id: str
    regime: str
    horizon_days: int
    expected_demand: float
    """Units the pair will really consume before a replenishment can land."""
    available_qty: float
    shortfall_qty: float
    will_run_short: bool
    """The label. A pair that will consume more than it holds needs action today."""
    planted: tuple[str, ...]
    """Scenario ids planted here, kept as context for the detail view. Not the label —
    see the module docstring for why."""
    in_scope: bool = True
    """Whether a pair-grain shortage scanner could act on this pair at all.

    False for parts the factory builds rather than buys. The horizon above is
    `purchase lead time + review period`, which is meaningless for an assembly: there is no
    purchase order to place and no supplier lead time to beat, so "will it run out before a
    replenishment can land" is being asked about a replenishment that does not exist. S1 skips
    these by design and S3 has nothing to transfer against a production plan; their shortage is
    S2's cascade, at the *parent* grain, which pair-keyed truth cannot judge.

    Measured before this existed: 73 of 111 "at risk" pairs were in-house assemblies, and they
    carried Rs 291 cr of the Rs 292 cr of "missed value" — so every money figure in the
    comparison was almost entirely an artefact of asking the wrong question about them.
    Excluded from scoring and counted instead, so the gap is disclosed rather than hidden.
    """


def _forward_seasonal_factor(amplitude: float, horizon_days: int) -> float:
    """Mean seasonal multiplier over the window starting tomorrow."""
    if amplitude == 0.0:
        return 1.0
    start = dates.TODAY.timetuple().tm_yday
    doy = (start + np.arange(1, horizon_days + 1)) % 365
    return float(demand._seasonal_factor(doy, amplitude).mean())


def purchasable_parts(supplier_performance) -> set[str]:
    """Parts with a preferred supplier contract — S1's universe, and the ERP arm's.

    The same restriction both arms already apply to themselves, lifted to the yardstick so the
    score is computed over the universe the arms are actually scanning.
    """
    if supplier_performance is None or supplier_performance.empty:
        return set()
    return set(supplier_performance[supplier_performance["IS_PREFERRED"]]["PART_ID"])


def build(position_rows, purchasable: set[str] | None = None) -> dict[tuple[str, str], PairTruth]:
    """Truth for every pair in the generated world, keyed by (part, warehouse).

    `purchasable` restricts what is *scored* — see `PairTruth.in_scope`. Pairs outside it are
    still labelled and returned, so the gap can be counted, but `scoring.score` skips them.
    Passing None scores everything, which is the pre-fix behaviour and is wrong for any figure
    shown to anybody; it is kept only so the labelling can be inspected on its own.
    """
    from agentic_restock.generation import scenarios

    params = demand.pair_params()
    out: dict[tuple[str, str], PairTruth] = {}

    for row in position_rows.itertuples(index=False):
        key = (row.PART_ID, row.WAREHOUSE_ID)
        p = params.get(key)
        if p is None:
            continue

        # The pair's own replenishment window, from the same policy the detectors use: a
        # part is short if it cannot cover demand until an order placed today would arrive.
        horizon = demand._lead_days_for(row.PART_ID) + policy.REVIEW_PERIOD_DAYS

        level = p.effective_level
        expected = level * _forward_seasonal_factor(p.seasonal_amplitude, horizon) * horizon

        available = float(row.QUANTITY_ON_HAND or 0) + float(row.IN_TRANSIT_QTY or 0)
        shortfall = max(expected - available, 0.0)

        out[key] = PairTruth(
            part_id=key[0],
            warehouse_id=key[1],
            regime=p.regime,
            horizon_days=horizon,
            expected_demand=float(expected),
            available_qty=available,
            shortfall_qty=float(shortfall),
            will_run_short=shortfall > 0,
            planted=tuple(scenarios.findings_for(*key)),
            in_scope=purchasable is None or key[0] in purchasable,
        )

    return out
