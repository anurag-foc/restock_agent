"""Policy inputs — the company settings the intelligence layer's figures depend on.

These are *not* measurements. Each one is a choice someone made, and every finding whose
numbers depend on one of them records it in `assumptions_used` so the PM sees it next to the
figure and can argue with it (docs/intelligence_layer_design.md §4 "Assumption disclosure").

Decided 2026-09-04 — basis in docs/intelligence_layer_design.md §5.1.
"""

# --- Holding cost -----------------------------------------------------------
#
# 14%/year, built bottom-up so every line is separately arguable. Deliberately NOT the
# industry-standard 20-30%: docs/market_evidence_phase1.md §8 traces that figure to a 1995
# trade article assuming 1990s interest rates, and it will not survive an informed buyer.
#
# COST_OF_CAPITAL_RATE is a placeholder for the customer's actual WACC.
# OBSOLESCENCE_RATE is the softest of the four -- real shrinkage is observable from inventory
# adjustment transactions, so it becomes measurable rather than assumed once there is history.

COST_OF_CAPITAL_RATE = 0.100
WAREHOUSING_RATE = 0.025
INSURANCE_RATE = 0.005
OBSOLESCENCE_RATE = 0.010

HOLDING_RATE = COST_OF_CAPITAL_RATE + WAREHOUSING_RATE + INSURANCE_RATE + OBSOLESCENCE_RATE
"""Annual cost of holding stock, as a fraction of stock value. 0.14."""

HOLDING_RATE_COMPONENTS = {
    "cost_of_capital": COST_OF_CAPITAL_RATE,
    "warehousing": WAREHOUSING_RATE,
    "insurance": INSURANCE_RATE,
    "obsolescence": OBSOLESCENCE_RATE,
}


# --- Service level ----------------------------------------------------------
#
# Tiered by criticality rather than one blanket figure. `dim_part` already carries
# CRITICALITY_CLASS and SAFETY_CRITICAL, so treating a brake component like a trim clip would
# be a choice rather than a data limitation.
#
# z is the standard normal quantile for the target availability.

SERVICE_LEVEL_BY_CLASS = {
    "A-CRITICAL": 0.99,
    "B": 0.95,
    "C": 0.90,
}

Z_BY_CLASS = {
    "A-CRITICAL": 2.33,
    "B": 1.65,
    "C": 1.28,
}

DEFAULT_CRITICALITY_CLASS = "B"


# --- Replenishment cycle ----------------------------------------------------
#
# The only genuine constant in FX3. `target_cover_days` is derived around it as
# `mu_lead + REVIEW_PERIOD_DAYS + (z * sigma_lead)` -- so an erratic supplier automatically
# forces more cover, using the same z and sigma_lead that price them in FX2. The two cannot
# drift apart into independently-tuned knobs.
#
# 7 days because nobody raises purchase orders twice a day, even though the board refreshes
# twice a day.

REVIEW_PERIOD_DAYS = 7


def normalise_criticality(criticality_class: str | None) -> str:
    """Map a raw CRITICALITY_CLASS value onto a service-level tier key.

    `dim_part` carries two unnormalised spellings of the same class ('A-CRITICAL' and
    'A - CRITICAL', roughly a 50/50 split) -- a legacy merge artifact. An exact match silently
    drops half the true A-CRITICAL parts onto the default tier, which would quietly give safety
    -critical components a 95% service level instead of 99%.
    """
    if not criticality_class:
        return DEFAULT_CRITICALITY_CLASS
    collapsed = criticality_class.replace(" ", "").upper()
    if collapsed == "A-CRITICAL":
        return "A-CRITICAL"
    if collapsed in ("B", "B-IMPORTANT"):
        return "B"
    if collapsed in ("C", "C-STANDARD"):
        return "C"
    return DEFAULT_CRITICALITY_CLASS


def z_for(criticality_class: str | None) -> float:
    """Service-level z for a part's criticality class."""
    return Z_BY_CLASS[normalise_criticality(criticality_class)]


def service_level_for(criticality_class: str | None) -> float:
    """Target availability for a part's criticality class."""
    return SERVICE_LEVEL_BY_CLASS[normalise_criticality(criticality_class)]


def target_cover_days(mu_lead: float, sigma_lead: float, criticality_class: str | None) -> float:
    """Days of cover to aim for when reordering -- derived, not a policy constant.

    `mu_lead + review period + (z * sigma_lead)`. The third term is what makes supplier
    inconsistency visible as stock: two suppliers with the same 40-day average lead time but
    different spreads produce materially different target cover, and the difference is a
    number a PM can take to a supplier meeting.
    """
    return mu_lead + REVIEW_PERIOD_DAYS + z_for(criticality_class) * sigma_lead


def excess_holding_cost(excess_qty: float, unit_cost: float, daily_burn: float) -> float:
    """Cost of carrying MOQ-forced excess, priced over how long it will actually take to consume.

    The superseded implementation applied a flat 2% of excess value with no time dimension,
    which understated this by roughly 4-5x on a part carrying several months of excess. See
    docs/intelligence_layer_design.md §5.1.
    """
    if excess_qty <= 0 or daily_burn <= 0:
        return 0.0
    excess_years = (excess_qty / daily_burn) / 365.0
    return excess_qty * unit_cost * HOLDING_RATE * excess_years
