"""Supplier contracts — `dim_supplier_contract`.

One contract per bought part (the 60 level-2 components; assemblies and sub-assemblies are
built in-house), plus extra contracts where the catalog needs competing suppliers.

Two things are deliberately embedded across the *whole* set rather than only in the finding
that showcases them:

- **Price is inversely related to reliability.** Each archetype carries a price factor, so the
  `cheap_and_bad` suppliers genuinely quote least. F5 is then the sharpest instance of a tension
  present dataset-wide, not a special case bolted onto one part — which matters, because a
  detector that only reverses the ranking on the one part we planted has not been tested.
- **MOQ regimes rotate.** Four regimes by position so `evaluate_feasibility`'s branches all see
  traffic: no real constraint, roughly-equal-to-need, a mild overbuy, and an awkward pack size
  that forces rounding.

`lead_time_days` here is the *contracted* figure — the number nobody has updated since go-live.
What suppliers actually do is generated separately in `supplier_facts.py`, and the gap between
the two is nuance 5.
"""

from __future__ import annotations

from functools import cache

from agentic_restock.generation.entities import SUPPLIER_ARCHETYPES, parts, suppliers
from agentic_restock.generation.scenarios import FINDINGS

# Reliable suppliers charge more. This is the dataset-wide version of F5's tension.
PRICE_FACTOR_BY_ARCHETYPE = {
    "tight": 1.10,
    "loose": 1.00,
    "drifting": 1.02,
    "improving": 1.05,
    "cheap_and_bad": 0.90,
    "untested": 1.00,
}

# (moq, pack_size) by rotating position -- see module docstring.
_MOQ_REGIMES = [
    (10, 1),    # MOQ well below any realistic need: no constraint
    (50, 1),    # MOQ roughly equal to need: marginal
    (200, 1),   # MOQ above need: a mild overbuy
    (60, 30),   # awkward pack: need 100 rounds to 120
]

_CONTRACTED_LEAD_DAYS = [20, 25, 30, 35, 40, 45, 50, 60]


@cache
def _bought_parts() -> list[dict]:
    """Components only — assemblies and sub-assemblies are manufactured, not purchased."""
    return [p for p in parts() if p["BOM_LEVEL"] == 2]


@cache
def _suppliable() -> list[dict]:
    return suppliers()


@cache
def contracts() -> list[dict]:
    """dim_supplier_contract rows."""
    rows: list[dict] = []
    supplier_list = _suppliable()
    f5 = FINDINGS["F5"]
    f6 = FINDINGS["F6"]
    f11 = FINDINGS["F11"]

    def price_for(part: dict, supplier: dict, override_factor: float | None = None) -> float:
        factor = override_factor or PRICE_FACTOR_BY_ARCHETYPE[supplier["_archetype"]]
        return round(float(part["UNIT_COST"]) * factor, 2)

    by_id = {s["SUPPLIER_ID"]: s for s in supplier_list}

    for index, part in enumerate(_bought_parts()):
        part_id = part["PART_ID"]

        # --- catalog overrides ---------------------------------------------
        if part_id == f5["part"]:
            # Three competing quotes: cheapest supplier is the least reliable, so
            # effective_unit_cost has to reverse the quoted-price ordering.
            for rank, (supplier_id, relative_price) in enumerate(f5["suppliers"]):
                supplier = by_id[supplier_id]
                rows.append(
                    _row(
                        part,
                        supplier,
                        lead_days=35,
                        moq=40,
                        pack_size=1,
                        unit_cost=price_for(part, supplier, relative_price),
                        is_preferred=rank == 0,  # today's choice is the cheap one
                    )
                )
            continue

        if part_id == f6["part"]:
            supplier = by_id[f6["supplier"]]
            rows.append(
                _row(
                    part,
                    supplier,
                    lead_days=30,
                    moq=f6["moq"],
                    pack_size=f6["pack_size"],
                    unit_cost=price_for(part, supplier),
                    is_preferred=True,
                )
            )
            continue

        if part_id == f11["high_exposure_part"]:
            # Long lead time is what makes the only available fix expensive, which is what
            # pushes this larger exposure below a smaller, transfer-fixable one.
            supplier = by_id[f11["high_exposure_supplier"]]
            rows.append(
                _row(
                    part,
                    supplier,
                    lead_days=f11["high_exposure_lead_days"],
                    moq=f11["high_exposure_moq"],
                    pack_size=1,
                    unit_cost=price_for(part, supplier),
                    is_preferred=True,
                )
            )
            continue

        if part_id in FINDINGS["F4"]["parts"]:
            # One part per erratic/drifting supplier, so nuance 5 has a named example of each.
            supplier_id = (
                FINDINGS["F4"]["erratic_supplier"]
                if part_id == FINDINGS["F4"]["parts"][0]
                else FINDINGS["F4"]["drifting_supplier"]
            )
            supplier = by_id[supplier_id]
            rows.append(
                _row(
                    part,
                    supplier,
                    lead_days=40,
                    moq=30,
                    pack_size=1,
                    unit_cost=price_for(part, supplier),
                    is_preferred=True,
                )
            )
            continue

        # --- default: one contract, rotating supplier and MOQ regime -------
        supplier = supplier_list[index % len(supplier_list)]
        moq, pack_size = _MOQ_REGIMES[index % len(_MOQ_REGIMES)]
        rows.append(
            _row(
                part,
                supplier,
                lead_days=_CONTRACTED_LEAD_DAYS[index % len(_CONTRACTED_LEAD_DAYS)],
                moq=moq,
                pack_size=pack_size,
                unit_cost=price_for(part, supplier),
                is_preferred=True,
            )
        )

    return rows


def _row(
    part: dict,
    supplier: dict,
    *,
    lead_days: int,
    moq: int,
    pack_size: int,
    unit_cost: float,
    is_preferred: bool,
) -> dict:
    return {
        "part_id": part["PART_ID"],
        "supplier_id": supplier["SUPPLIER_ID"],
        "lead_time_days": lead_days,
        "moq": moq,
        "pack_size": pack_size,
        "unit_cost": unit_cost,
        "is_preferred": is_preferred,
    }


@cache
def _contract_index() -> dict[tuple[str, str], dict]:
    return {(r["part_id"], r["supplier_id"]): r for r in contracts()}


@cache
def _preferred_index() -> dict[str, str]:
    return {r["part_id"]: r["supplier_id"] for r in contracts() if r["is_preferred"]}


def contract_for(part_id: str, supplier_id: str) -> dict | None:
    return _contract_index().get((part_id, supplier_id))


def contracted_lead_days(part_id: str, supplier_id: str) -> int | None:
    row = contract_for(part_id, supplier_id)
    return row["lead_time_days"] if row else None


def preferred_supplier(part_id: str) -> str | None:
    """Indexed, not scanned: this is called once per simulated reorder (~41K times), and a
    linear scan over 62 contracts rebuilt on every call dominated the whole generator."""
    return _preferred_index().get(part_id)


@cache
def contracted_pairs() -> list[tuple[str, str]]:
    """(part_id, supplier_id) pairs with a contract — the universe for delivery history."""
    return [(r["part_id"], r["supplier_id"]) for r in contracts()]


@cache
def archetype_of(supplier_id: str) -> str:
    return next(s["_archetype"] for s in suppliers() if s["SUPPLIER_ID"] == supplier_id)


@cache
def true_lead_parameters(supplier_id: str, contracted_days: int) -> tuple[float, float]:
    """The supplier's TRUE mean and spread, from its archetype. Recorded in ground truth.

    This is what Phase 2 measures the lead-time estimator against — the estimator sees only
    delivery dates and has to recover these.
    """
    offset, sigma, _reject, _n = SUPPLIER_ARCHETYPES[archetype_of(supplier_id)]
    return contracted_days + offset, sigma


# The `improving` archetype's recent behaviour, and how far back "recent" reaches.
IMPROVED_OFFSET_DAYS = 1.0
IMPROVEMENT_WINDOW_DAYS = 180


def true_lead_parameters_at(
    supplier_id: str, contracted_days: int, days_ago: float
) -> tuple[float, float]:
    """Lead-time parameters as they were `days_ago`, for archetypes that change over time.

    Only `improving` varies: chronically late historically, near contract for the last six
    months. Without this the archetype is indistinguishable from `drifting`, and E2's recency
    weighting — the thing that stops a supplier being condemned for last year's performance —
    has nothing to detect.
    """
    mu, sigma = true_lead_parameters(supplier_id, contracted_days)
    if archetype_of(supplier_id) == "improving" and days_ago <= IMPROVEMENT_WINDOW_DAYS:
        return contracted_days + IMPROVED_OFFSET_DAYS, sigma
    return mu, sigma


@cache
def reject_rate(supplier_id: str) -> float:
    _offset, _sigma, rate, _n = SUPPLIER_ARCHETYPES[archetype_of(supplier_id)]
    return rate
