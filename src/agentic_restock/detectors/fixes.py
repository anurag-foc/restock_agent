"""FX1-FX3 — constructing a fix, and pricing it honestly.

These three are why `action_cost` stops being a percentage of exposure. The superseded ranking
used `exposure × 0.03` for a transfer, `× 0.15–0.50` for a buy and `× 1.00` for nothing — a
plausible-looking number available for every candidate before any option was chosen, which is
exactly why the model kept presenting it to PMs as a price. Three prose fixes failed to stop
that. Here the cost comes from a real orderable quantity at a real effective unit cost, so there
is no fabrication-bait figure left in scope.

Every function returns the fields a report needs to *show its working*, not just a total. The
grounding rules that survived production are all of the same shape: a verbatim field dump has no
editorial latitude, a summary does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from agentic_restock.estimators import risk
from agentic_restock.generation import policy

# A donor may not be pushed below this multiple of its own safety stock. Protecting the donor at
# exactly safety stock would make every transfer look free while quietly moving the shortage.
DONOR_PROTECTION_MULTIPLE = 1.0

# A location is a transfer candidate when its cover is below this multiple of its lead time --
# i.e. it will run out before a replenishment could land.
NEEDY_COVER_MULTIPLE = 1.0

# A donor must hold at least this much cover after giving, or it becomes the next finding.
DONOR_MIN_COVER_AFTER_DAYS = 21.0


@dataclass(frozen=True)
class TransferOption:
    """FX1 — move owned stock. Spends freight, not purchase price."""

    part_id: str
    receiver_warehouse_id: str
    donor_warehouse_id: str
    transfer_qty: int
    freight_cost: float
    receiver_risk_before: float
    receiver_risk_after: float
    donor_risk_before: float
    donor_risk_after: float
    donor_cover_after_days: float
    benefit: float
    action_cost: float
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SupplierOption:
    """FX2 — which supplier, priced so unreliability is visible in rupees."""

    part_id: str
    supplier_id: str
    quoted_unit_cost: float
    reject_rate: float
    sigma_lead_days: float
    variance_premium: float
    effective_unit_cost: float
    lead_tier: str
    observations: int
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PurchaseOption:
    """FX3 — how much to buy. The quantity is computed, never chosen."""

    part_id: str
    supplier_id: str
    required_qty: int
    orderable_qty: int
    moq: int
    pack_size: int
    excess_qty: int
    excess_months: float
    excess_holding_cost: float
    effective_unit_cost: float
    subtotal: float
    action_cost: float
    is_uneconomic: bool
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# FX1 -- lateral transfer
# ---------------------------------------------------------------------------


def _risk_at(
    *, forward_burn: float, sigma_d: float, mu_lead: float, sigma_lead: float, available: float
) -> float:
    probability, _, _ = risk.stockout_probability(
        forward_burn=forward_burn,
        sigma_d=sigma_d,
        mu_lead=mu_lead,
        sigma_lead=sigma_lead,
        available_qty=available,
    )
    return probability


def rank_transfer_options(
    *,
    part_id: str,
    receiver: dict,
    candidates: list[dict],
    freight_cost: float,
) -> list[TransferOption]:
    """Every donor that could cover the receiver, best first.

    Ranked on **cover given up**, not on quantity held. The largest pile is often the wrong
    donor: a warehouse holding more units but burning faster has less protected surplus, and a
    detector that sorts donors by quantity picks it. `receiver` and each candidate carry the
    corrected burn, spread and lead time from `part_position`.

    Benefit is the change in `P(stockout) × consequence` at the receiver *minus* the same change
    at the donor. That is what makes donor protection quantitative instead of a hardcoded buffer
    — moving a shortage is not a fix.
    """
    # Top up to the service level the part's criticality class actually asks for, NOT to mean
    # demand over the lead time. Covering the mean leaves you at the median of the distribution,
    # so `receiver_risk_after` is then forced to ~0.50 by construction -- measured at 75 of 92
    # transfers landing at exactly 0.50 on the first live run, while the same report printed
    # `service_level = 99% (A-CRITICAL)` on its ASSUMPTIONS line. The safety term is what makes
    # the two agree, and it uses the same z and sigma_lead that FX2 prices a supplier's
    # unpredictability with, so an erratic lead time widens the ask here too.
    mu_dl, sigma_dl = risk.demand_over_lead(
        forward_burn=receiver["forward_burn"],
        sigma_d=receiver["sigma_d"],
        mu_lead=receiver["mu_lead"],
        sigma_lead=receiver["sigma_lead"],
    )
    z = policy.z_for(receiver.get("criticality_class"))
    need = max(math.ceil(mu_dl + z * sigma_dl) - receiver["available_qty"], 0)
    if need <= 0:
        return []

    options: list[TransferOption] = []
    for donor in candidates:
        if donor["warehouse_id"] == receiver["warehouse_id"]:
            continue

        protected = donor["safety_stock_qty"] * DONOR_PROTECTION_MULTIPLE
        spare = donor["available_qty"] - protected
        if spare <= 0:
            continue

        qty = int(min(need, spare))
        if qty <= 0:
            continue

        donor_burn = max(donor["forward_burn"], 1e-9)
        cover_after = (donor["available_qty"] - qty) / donor_burn
        if cover_after < DONOR_MIN_COVER_AFTER_DAYS:
            # Giving this much would make the donor the next finding.
            qty = int(max(0, donor["available_qty"] - DONOR_MIN_COVER_AFTER_DAYS * donor_burn))
            if qty <= 0:
                continue
            cover_after = (donor["available_qty"] - qty) / donor_burn

        receiver_before = _risk_at(
            forward_burn=receiver["forward_burn"],
            sigma_d=receiver["sigma_d"],
            mu_lead=receiver["mu_lead"],
            sigma_lead=receiver["sigma_lead"],
            available=receiver["available_qty"],
        )
        receiver_after = _risk_at(
            forward_burn=receiver["forward_burn"],
            sigma_d=receiver["sigma_d"],
            mu_lead=receiver["mu_lead"],
            sigma_lead=receiver["sigma_lead"],
            available=receiver["available_qty"] + qty,
        )
        donor_before = _risk_at(
            forward_burn=donor["forward_burn"],
            sigma_d=donor["sigma_d"],
            mu_lead=donor["mu_lead"],
            sigma_lead=donor["sigma_lead"],
            available=donor["available_qty"],
        )
        donor_after = _risk_at(
            forward_burn=donor["forward_burn"],
            sigma_d=donor["sigma_d"],
            mu_lead=donor["mu_lead"],
            sigma_lead=donor["sigma_lead"],
            available=donor["available_qty"] - qty,
        )

        benefit = (receiver_before - receiver_after) * receiver["consequence"] - (
            donor_after - donor_before
        ) * donor["consequence"]

        options.append(
            TransferOption(
                part_id=part_id,
                receiver_warehouse_id=receiver["warehouse_id"],
                donor_warehouse_id=donor["warehouse_id"],
                transfer_qty=qty,
                freight_cost=float(freight_cost),
                receiver_risk_before=receiver_before,
                receiver_risk_after=receiver_after,
                donor_risk_before=donor_before,
                donor_risk_after=donor_after,
                donor_cover_after_days=cover_after,
                benefit=benefit,
                action_cost=float(freight_cost),
                evidence={
                    # The quantity and the donor have to be IN the evidence, not only on the
                    # dataclass: the persisted line reads its quantity from here, and a transfer
                    # approved for zero units is worse than no transfer at all.
                    "transfer_qty": qty,
                    "donor_warehouse_id": donor["warehouse_id"],
                    "receiver_warehouse_id": receiver["warehouse_id"],
                    "receiver_consequence": round(float(receiver["consequence"]), 2),
                    "receiver_available": receiver["available_qty"],
                    # Named for what it IS -- the gap, not the requirement. As `receiver_need` a
                    # live run wrote "WH002 has 3676 units against a need of 1755", which reads
                    # as comfortably stocked and makes the transfer look pointless. The field is
                    # the only thing the model sees, so the field has to say what it means.
                    "receiver_short_by": need,
                    "donor_available": donor["available_qty"],
                    "donor_safety_stock": donor["safety_stock_qty"],
                    "donor_burn_per_day": round(donor["forward_burn"], 2),
                    "donor_cover_after_days": round(cover_after, 1),
                    "freight_cost": round(float(freight_cost), 2),
                },
            )
        )

    # Best donor = most benefit, then the one giving up the least cover.
    return sorted(options, key=lambda o: (-o.benefit, -o.donor_cover_after_days))


# ---------------------------------------------------------------------------
# FX2 -- supplier economics
# ---------------------------------------------------------------------------


def effective_unit_cost(
    *,
    quoted_unit_cost: float,
    reject_rate: float,
    sigma_lead_days: float,
    forward_burn: float,
    unit_cost: float,
    criticality_class: str | None,
) -> tuple[float, float]:
    """Quoted price adjusted for rejects and for the buffer this supplier's spread forces.

    Returns (effective_unit_cost, variance_premium).

    The premium is the carrying cost of the extra safety stock needed to absorb `sigma_lead`:
    `holding_rate × unit_cost × (z × sigma_lead × burn)`. That is what turns "unreliable" into a
    number procurement can argue with — a 0-100 reliability score is unarguable, and a reversed
    cost ranking is not.
    """
    z = policy.z_for(criticality_class)
    extra_units = z * max(sigma_lead_days, 0.0) * max(forward_burn, 0.0)
    premium = policy.HOLDING_RATE * float(unit_cost) * extra_units
    scrap_adjusted = float(quoted_unit_cost) * (1.0 + max(reject_rate, 0.0))

    # Spread the annual carrying cost of that buffer over the units bought in a year, so it can
    # be compared against a quoted price at all.
    #
    # NOT divided by `extra_units`: that cancels sigma_lead entirely and leaves
    # `holding_rate x unit_cost` -- a constant identical for every supplier, contributing nothing
    # to the ranking. The premium existed but priced nothing, and the reversal it is supposed to
    # produce could never happen.
    annual_units = max(forward_burn, 0.0) * 365.0
    per_unit_premium = premium / annual_units if annual_units > 0 else 0.0
    return scrap_adjusted + per_unit_premium, premium


def rank_suppliers(
    *,
    part_id: str,
    quotes: list[dict],
    forward_burn: float,
    unit_cost: float,
    criticality_class: str | None,
) -> list[SupplierOption]:
    """Cheapest *effective* cost first — which need not be the cheapest quote."""
    options: list[SupplierOption] = []
    for quote in quotes:
        effective, premium = effective_unit_cost(
            quoted_unit_cost=quote["quoted_unit_cost"],
            reject_rate=quote.get("reject_rate", 0.0),
            sigma_lead_days=quote.get("sigma_lead_days", 0.0),
            forward_burn=forward_burn,
            unit_cost=unit_cost,
            criticality_class=criticality_class,
        )
        options.append(
            SupplierOption(
                part_id=part_id,
                supplier_id=quote["supplier_id"],
                quoted_unit_cost=float(quote["quoted_unit_cost"]),
                reject_rate=float(quote.get("reject_rate", 0.0)),
                sigma_lead_days=float(quote.get("sigma_lead_days", 0.0)),
                variance_premium=premium,
                effective_unit_cost=effective,
                lead_tier=quote.get("lead_tier", "unknown"),
                observations=int(quote.get("observations", 0)),
                evidence={
                    # The id has to be in the evidence, not just on the dataclass: the persisted
                    # row needs it to record WHICH supplier is being recommended, and a
                    # re-sourcing decision with a null recommendation is not decidable.
                    "supplier_id": quote["supplier_id"],
                    "quoted_unit_cost": round(float(quote["quoted_unit_cost"]), 2),
                    "reject_rate": round(float(quote.get("reject_rate", 0.0)), 4),
                    "sigma_lead_days": round(float(quote.get("sigma_lead_days", 0.0)), 2),
                    "effective_unit_cost": round(effective, 2),
                    "lead_tier": quote.get("lead_tier", "unknown"),
                    "observations": int(quote.get("observations", 0)),
                },
            )
        )
    return sorted(options, key=lambda o: o.effective_unit_cost)


# ---------------------------------------------------------------------------
# FX3 -- orderable quantity
# ---------------------------------------------------------------------------

# When MOQ-forced excess costs more than this share of the exposure being avoided, buying is the
# wrong action and renegotiating the pack is the finding.
UNECONOMIC_EXCESS_SHARE = 0.25


def build_purchase_option(
    *,
    part_id: str,
    supplier_id: str,
    target_cover_days: float,
    forward_burn: float,
    available_qty: int,
    moq: int,
    pack_size: int,
    effective_unit_cost_per_unit: float,
    unit_cost: float,
    exposure: float,
) -> PurchaseOption:
    """How much to buy, and what that actually costs.

    **The quantity is computed here, never chosen elsewhere.** Letting a caller supply it is what
    silently disabled the MOQ constraint: `evaluate_feasibility` passes through any quantity at
    or above MOQ with zero excess, so asking for a big number made the constraint vanish from
    the report entirely. One live quote recommended 910 units of a part whose target level was
    188 for exactly that reason.

    The excess holding cost is priced over how long the excess will actually take to consume, not
    as a flat percentage — the superseded flat 2% understated a multi-month overbuy 4-5x.
    """
    required = max(math.ceil(target_cover_days * forward_burn) - available_qty, 0)
    pack = max(int(pack_size), 1)
    orderable = max(int(moq), int(math.ceil(required / pack) * pack)) if required > 0 else 0

    excess = max(orderable - required, 0)
    excess_months = (excess / forward_burn / 30.0) if forward_burn > 0 else 0.0
    holding = policy.excess_holding_cost(excess, unit_cost, forward_burn)

    subtotal = orderable * float(effective_unit_cost_per_unit)
    action_cost = subtotal + holding

    uneconomic = bool(exposure > 0 and holding > exposure * UNECONOMIC_EXCESS_SHARE)

    return PurchaseOption(
        part_id=part_id,
        supplier_id=supplier_id,
        required_qty=required,
        orderable_qty=orderable,
        moq=int(moq),
        pack_size=pack,
        excess_qty=excess,
        excess_months=excess_months,
        excess_holding_cost=holding,
        effective_unit_cost=float(effective_unit_cost_per_unit),
        subtotal=subtotal,
        action_cost=action_cost,
        is_uneconomic=uneconomic,
        # Every field in order, so a report can dump it verbatim. A summary invites the model to
        # fill an optional slot from whatever number is nearest to hand; a dump does not.
        evidence={
            "target_cover_days": round(target_cover_days, 1),
            "forward_burn": round(forward_burn, 2),
            "available_qty": int(available_qty),
            "required_qty": required,
            "moq": int(moq),
            "pack_size": pack,
            "orderable_qty": orderable,
            "excess_qty": excess,
            "excess_months": round(excess_months, 1),
            "excess_holding_cost": round(holding, 2),
            "effective_unit_cost": round(float(effective_unit_cost_per_unit), 2),
            "subtotal": round(subtotal, 2),
            "action_cost": round(action_cost, 2),
        },
    )
