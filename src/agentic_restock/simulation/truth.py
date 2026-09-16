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


def _forward_seasonal_factor(amplitude: float, horizon_days: int) -> float:
    """Mean seasonal multiplier over the window starting tomorrow."""
    if amplitude == 0.0:
        return 1.0
    start = dates.TODAY.timetuple().tm_yday
    doy = (start + np.arange(1, horizon_days + 1)) % 365
    return float(demand._seasonal_factor(doy, amplitude).mean())


def build(position_rows) -> dict[tuple[str, str], PairTruth]:
    """Truth for every pair in the generated world, keyed by (part, warehouse)."""
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
        )

    return out
