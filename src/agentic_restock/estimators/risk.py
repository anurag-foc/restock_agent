"""R — stockout probability and exposure.

The single biggest change in the redesign: **exposure is `P(stockout) × consequence`**, not
`shortfall_qty × unit_cost`.

Pricing a shortage at the cost of buying the missing parts measures the wrong thing entirely. A
₹40 fastener that halts an engine line is not a ₹4,000 problem. And a deterministic shortfall
cannot express likelihood at all, so it ranks a near-certain small loss below a remote large one
— which is exactly backwards for someone deciding what to do this morning.

Both uncertainties are combined, which is why the estimators had to report a spread rather than
a point:

    mu_DL    = forward_burn x mu_lead
    sigma_DL = sqrt( mu_lead x sigma_d^2  +  forward_burn^2 x sigma_lead^2 )
    P        = Phi( (mu_DL - available) / sigma_DL )

A useful property falls out of that formula rather than needing a rule: when burn confidence is
LOW, `sigma_d` is large, so `sigma_DL` is large and `P` is pulled toward 0.5. Poorly-understood
parts are automatically ranked with less conviction instead of being handed false certainty.

Phi comes from `math.erf`, which is exact. The design sketched an Abramowitz-Stegun
approximation for use inside SQL; since the estimators ended up in Python -- which is what makes
the accuracy gate a local test -- the approximation is unnecessary and would only add error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from agentic_restock.generation import policy

# Consequence for a part with no planned assembly behind it. THE WEAKEST NUMBER IN THE SYSTEM:
# there is no downtime-cost or production-value data for these parts, so it is a
# criticality-weighted multiple of the demand that would go unserved. It must be labelled as an
# estimate wherever it is shown, never dressed as a measured figure
# (docs/intelligence_layer_design.md §6.1).
#
# Scaled by FLOW -- the value of demand across the recovery window -- not by holdings. An earlier
# version used `unit_cost x safety_stock x multiple`, which is backwards: holding more stock
# cannot increase what it costs to run out. On a high-value assembly with a deep buffer that
# produced a ₹218 crore consequence for a single part.
CONSEQUENCE_MULTIPLE_BY_CLASS = {
    "A-CRITICAL": 6.0,
    "B": 2.0,
    "C": 1.0,
}

# Confidence below which a stockout probability should not be presented as precise.
LOW_CONFIDENCE = "LOW"

# Floor on sigma_DL so a zero-variance series cannot produce a 0/1 step function. A part with no
# observed variability still has some, and we have simply not seen it.
MIN_SIGMA_DL_FRACTION = 0.05


@dataclass(frozen=True)
class StockoutRisk:
    p_stockout: float
    mu_demand_over_lead: float
    sigma_demand_over_lead: float
    available_qty: int
    consequence: float
    consequence_basis: str
    exposure: float
    confidence: str

    @property
    def is_estimated_consequence(self) -> bool:
        """True when the consequence is a criticality proxy rather than planned production."""
        return self.consequence_basis == "CRITICALITY_PROXY"


def normal_cdf(z: float) -> float:
    """Phi(z), exact to double precision via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def demand_over_lead(
    *, forward_burn: float, sigma_d: float, mu_lead: float, sigma_lead: float
) -> tuple[float, float]:
    """Mean and standard deviation of demand across the replenishment lead time.

    The variance term combines both sources: demand varying over a fixed lead time, and a fixed
    demand rate over a varying lead time. Dropping the second is the usual mistake, and it is
    what makes an erratic supplier look as safe as a consistent one.
    """
    mu = forward_burn * mu_lead
    variance = mu_lead * (sigma_d**2) + (forward_burn**2) * (sigma_lead**2)
    return mu, math.sqrt(max(variance, 0.0))


def stockout_probability(
    *,
    forward_burn: float,
    sigma_d: float,
    mu_lead: float,
    sigma_lead: float,
    available_qty: float,
) -> tuple[float, float, float]:
    """P(demand over the lead time exceeds what is available), with the moments used."""
    mu, sigma = demand_over_lead(
        forward_burn=forward_burn, sigma_d=sigma_d, mu_lead=mu_lead, sigma_lead=sigma_lead
    )

    if forward_burn <= 0:
        # Nothing is moving, so nothing can run out. Dead capital is a different finding.
        return 0.0, mu, sigma

    floor = MIN_SIGMA_DL_FRACTION * mu
    sigma_used = max(sigma, floor) if mu > 0 else sigma
    if sigma_used <= 0:
        return (1.0 if mu > available_qty else 0.0), mu, sigma

    return normal_cdf((mu - available_qty) / sigma_used), mu, sigma


def consequence_of_stockout(
    *,
    unit_cost: float,
    unserved_units: float,
    criticality_class: str | None,
    cascade_value_at_risk: float | None = None,
) -> tuple[float, str]:
    """What it costs if this part runs out, and on what basis.

    Planned production wins whenever it exists: a part blocking an A-CRITICAL assembly is worth
    the output it blocks, and that figure is measured rather than assumed. Everything else falls
    back to a criticality proxy over `unserved_units` -- the demand that would go unmet across
    the recovery window -- which is flagged as such. See the module constant for why this scales
    with flow and not with holdings.
    """
    if cascade_value_at_risk is not None and cascade_value_at_risk > 0:
        return float(cascade_value_at_risk), "PLANNED_PRODUCTION"

    tier = policy.normalise_criticality(criticality_class)
    multiple = CONSEQUENCE_MULTIPLE_BY_CLASS[tier]
    return float(unit_cost) * max(unserved_units, 1.0) * multiple, "CRITICALITY_PROXY"


def assess(
    *,
    forward_burn: float,
    sigma_d: float,
    burn_confidence: str,
    mu_lead: float,
    sigma_lead: float,
    available_qty: int,
    unit_cost: float,
    safety_stock_qty: int,
    criticality_class: str | None,
    cascade_value_at_risk: float | None = None,
) -> StockoutRisk:
    """Full risk assessment for one (part, warehouse)."""
    p_stockout, mu, sigma = stockout_probability(
        forward_burn=forward_burn,
        sigma_d=sigma_d,
        mu_lead=mu_lead,
        sigma_lead=sigma_lead,
        available_qty=available_qty,
    )
    # Unserved demand across the recovery window: what the lead time would have consumed beyond
    # what is on hand. Bounded below by a single lead-time's demand so a currently-covered part
    # still carries a meaningful consequence if it does run out.
    unserved = max(mu - available_qty, 0.0) or mu
    consequence, basis = consequence_of_stockout(
        unit_cost=unit_cost,
        unserved_units=unserved,
        criticality_class=criticality_class,
        cascade_value_at_risk=cascade_value_at_risk,
    )

    return StockoutRisk(
        p_stockout=p_stockout,
        mu_demand_over_lead=mu,
        sigma_demand_over_lead=sigma,
        available_qty=int(available_qty),
        consequence=consequence,
        consequence_basis=basis,
        exposure=p_stockout * consequence,
        confidence=burn_confidence,
    )


def shortfall_exposure(
    *, safety_stock_qty: int, available_qty: int, unit_cost: float
) -> float:
    """The SUPERSEDED formula, kept for the parallel-run comparison.

    `shortfall x unit_cost` — the cost of buying the missing parts. Retained only so a run can
    show the two orderings side by side and answer whether decision-value ranking actually picks
    different actions (market_evidence_phase1.md §16). Not used to rank anything.
    """
    return max(safety_stock_qty - available_qty, 0) * float(unit_cost)
