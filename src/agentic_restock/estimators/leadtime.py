"""E2 — lead time, from delivery history.

The second estimator every other number inherits. What matters here is **spread, not just
drift**: 40 days ± 2 against a 40-day contract is fine, 40 ± 14 is unmanageable with drift
exactly zero, and a mean-only check sees nothing wrong with the second. `sigma_lead` is what
drives the safety stock a supplier forces you to carry, and it is what makes them expensive
(docs/intelligence_layer_design.md §2, FX2).

Two mechanisms, both there because the alternative is quietly wrong:

**A hierarchical fallback**, because most (supplier, part) pairs will never have enough
deliveries to fit. Lead time is genuinely a property of the pair — a supplier can be reliable on
a commodity fastener and chronically late on a machined casting, and averaging them describes
neither — so the pair tier is preferred whenever it is reachable, and the estimate always
reports which tier it actually used and on how many observations.

**Exponential recency weighting**, because a supplier late last year but clean for six months is
not currently drifting. Without it the `improving` archetype is indistinguishable from the
`drifting` one, and a supplier gets condemned for history they have already fixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Minimum observations to fit at each tier. The pair tier needs more because it is the most
# specific claim; below these the estimate falls through to a broader pool.
MIN_OBSERVATIONS_PAIR = 8
MIN_OBSERVATIONS_SUPPLIER = 3
MIN_OBSERVATIONS_CATEGORY = 3

# Recency half-life. At 180 days an observation carries half the weight of one made today.
RECENCY_HALF_LIFE_DAYS = 180.0

# z for the 90th percentile of a normal. Used to project P90 lead time from mean and spread
# rather than reading an empirical quantile, which is unstable on samples this small.
Z_P90 = 1.2816

TIER_PAIR = "supplier_part"
TIER_SUPPLIER = "supplier"
TIER_CATEGORY = "supplier_category"
TIER_CONTRACTED = "contracted"


@dataclass(frozen=True)
class DeliveryObservation:
    """One captured delivery: how late it was, and how long ago."""

    part_id: str
    supplier_id: str
    supplier_category: str
    delay_days: float
    days_ago: float


@dataclass(frozen=True)
class LeadTimeEstimate:
    mu_lead: float
    """Expected lead time in days — contracted plus the weighted mean delay."""

    sigma_lead: float
    """Recency-weighted standard deviation of the lead time. The number that matters most."""

    p90_lead: float
    observations: int
    tier: str
    mean_delay: float
    otd_rate: float

    sigma_unweighted: float
    """Plain sample standard deviation. Reported so a gate can compare like with like against a
    ground-truth figure that has no notion of recency, without weakening the weighted estimate
    that production actually uses."""

    @property
    def is_measured(self) -> bool:
        return self.tier != TIER_CONTRACTED

    @property
    def drift_days(self) -> float:
        """How much later than contract this supplier actually runs."""
        return self.mean_delay


def _weights(
    days_ago: np.ndarray, half_life_days: float = RECENCY_HALF_LIFE_DAYS
) -> np.ndarray:
    return np.power(0.5, days_ago / max(half_life_days, 1e-9))


def _weighted_moments(values: np.ndarray, weights: np.ndarray) -> tuple[float, float]:
    """Weighted mean and standard deviation, with the reliability correction for weights."""
    total = weights.sum()
    if total <= 0:
        return 0.0, 0.0
    mean = float((values * weights).sum() / total)
    if len(values) < 2:
        return mean, 0.0
    # Effective sample size keeps a handful of heavily-weighted points from reporting an
    # implausibly tight spread.
    effective = (total**2) / float((weights**2).sum())
    if effective <= 1:
        return mean, 0.0
    variance = float((weights * (values - mean) ** 2).sum() / total)
    return mean, math.sqrt(variance * effective / (effective - 1))


def _select_pool(
    observations: list[DeliveryObservation],
    *,
    part_id: str,
    supplier_id: str,
    supplier_category: str,
) -> tuple[list[DeliveryObservation], str]:
    """First tier of the ladder with enough observations to fit."""
    pair = [o for o in observations if o.part_id == part_id and o.supplier_id == supplier_id]
    if len(pair) >= MIN_OBSERVATIONS_PAIR:
        return pair, TIER_PAIR

    supplier = [o for o in observations if o.supplier_id == supplier_id]
    if len(supplier) >= MIN_OBSERVATIONS_SUPPLIER:
        return supplier, TIER_SUPPLIER

    category = [o for o in observations if o.supplier_category == supplier_category]
    if len(category) >= MIN_OBSERVATIONS_CATEGORY:
        return category, TIER_CATEGORY

    return [], TIER_CONTRACTED


def estimate_lead_time(
    observations: list[DeliveryObservation],
    *,
    part_id: str,
    supplier_id: str,
    supplier_category: str,
    contracted_days: float,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS,
) -> LeadTimeEstimate:
    """Estimate lead time for one (part, supplier), falling back through broader pools.

    `observations` is the whole captured set; this filters it. Passing everything rather than a
    pre-grouped structure keeps the tier choice inside the estimator, where the gate can check
    that it picked the tier the data supports.
    """
    pool, tier = _select_pool(
        observations,
        part_id=part_id,
        supplier_id=supplier_id,
        supplier_category=supplier_category,
    )

    if not pool:
        # Nothing observed. The contract is all there is, and reporting zero spread would be a
        # false claim of certainty -- but so would inventing one, so it is left at zero and the
        # tier says why.
        return LeadTimeEstimate(
            mu_lead=float(contracted_days),
            sigma_lead=0.0,
            p90_lead=float(contracted_days),
            observations=0,
            tier=TIER_CONTRACTED,
            mean_delay=0.0,
            otd_rate=0.0,
            sigma_unweighted=0.0,
        )

    delays = np.array([o.delay_days for o in pool], dtype=float)
    ages = np.array([o.days_ago for o in pool], dtype=float)

    mean_delay, sigma = _weighted_moments(delays, _weights(ages, half_life_days))
    mu_lead = float(contracted_days) + mean_delay

    return LeadTimeEstimate(
        mu_lead=mu_lead,
        sigma_lead=sigma,
        p90_lead=mu_lead + Z_P90 * sigma,
        observations=len(pool),
        tier=tier,
        mean_delay=mean_delay,
        otd_rate=float((delays <= 0).mean()),
        sigma_unweighted=float(delays.std(ddof=1)) if len(delays) > 1 else 0.0,
    )


def expected_tier(pair_observations: int) -> str:
    """Which tier a pair with this many observations should resolve at.

    Mirrors `_select_pool`'s pair-tier test only — the broader tiers depend on pools this does
    not see. Exists so the generator can record the expectation and the gate can compare.
    """
    if pair_observations >= MIN_OBSERVATIONS_PAIR:
        return TIER_PAIR
    if pair_observations >= MIN_OBSERVATIONS_SUPPLIER:
        return TIER_SUPPLIER
    if pair_observations >= 1:
        return TIER_CATEGORY
    return TIER_CONTRACTED
