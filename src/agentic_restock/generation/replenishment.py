"""Replenishment — the reorder loop, and steering each pair to its finding condition.

Two jobs, and they are in tension:

1. **Emergent realism.** Stock positions should arise from a reorder-and-arrive loop against the
   generated demand, not be asserted. Stockouts that happen because a long lead time collided
   with a demand spike are worth more than stockouts we wrote down.
2. **Exact terminal conditions.** The findings need precise values *today*: F6 needs a
   replenishment gap of about 60 units so MOQ 500 is a ~7-month overbuy, F1 needs a decoy donor
   holding more units but less cover than the real donor, F3 needs a part below its own safety
   stock. "Roughly short" does not test a detector.

Resolution: run the loop naturally over the whole window to get a *receipt schedule*, then solve
for the **opening balance** that makes the closing balance land exactly on the target
(`opening = target + total_issues - total_receipts`). The receipt timings stay emergent; only the
starting quantity, 1100 days ago, is chosen. `_lift_troughs` then moves receipt mass earlier
where the balance would go negative, which cannot disturb the closing value.

Reconciliation is structural, not checked: the on-hand series is *defined* as
`opening - cumsum(issues) + cumsum(receipts)`, so `on_hand[t] == on_hand[t-1] - issues[t] +
receipts[t]` cannot fail. The generator asserts non-negativity instead, which is the property
that can actually break.

**Accepted simplification: no historical stockouts.** `_lift_troughs` keeps every balance
non-negative, which means issued quantity always equals demand and the ISSUE series is an
unbiased record of demand. The alternative — censoring issues at available stock, which is what
physically happens — would be more realistic but makes the burn estimator's target ambiguous:
it would see *fulfilled* demand while ground truth recorded *true* demand, and the gap would be
a property of the reorder policy rather than of the estimator. Censored demand is a real problem
in inventory analytics and worth modelling eventually; it is not what Phase 2 is trying to
measure. Parts are short *today* where a finding needs them short — that part is not simplified.

The receipt events produced here are also the source for `fact_supplier_delivery` (module 8), so
every recorded delivery is an event that genuinely moved stock — the delivery history and the
inventory history describe the same world rather than two unrelated simulations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agentic_restock.generation import bom, contracts, demand, policy
from agentic_restock.generation.demand import PairParams
from agentic_restock.generation.scenarios import FINDINGS

# F1's donor pair. The decoy must end up holding MORE units than the real donor while having
# LESS protected cover, which is only true if its own burn rate is higher -- asserted in the
# tests rather than assumed, since both burns come from the demand model.
F1_BEST_DONOR_COVER_DAYS = 75
F1_DECOY_COVER_DAYS = 50

# F2's parent can be built this many times from either binding child's stock -- identical for
# both, which is what makes them co-binding.
F2_BUILDABLE_UNITS = 1200  # partial block: the 14-day plan needs ~1900

# Own stock held by a cascade parent at the production site. Small but non-zero: a real plant
# holds some finished assemblies, and zero would make the block total rather than partial.
CASCADE_PARENT_ON_HAND = 150

# Own stock held by a sub-assembly on a cascade path. Lower than the parent's, because the
# constraint has to reach past it down to the components the finding names.
CASCADE_INTERMEDIATE_ON_HAND = 40


def _cascade_intermediates() -> frozenset[str]:
    """Parts between a cascade parent and its planted binding children.

    Derived from the BOM rather than listed, so re-parenting a component in `bom.py` cannot
    silently leave an intermediate uncapped and disarm the finding.
    """
    f = FINDINGS
    targets: set[str] = set()
    for finding, children in (
        (f["F2"], f["F2"]["binding_children"]),
        (f["F3"], [f["F3"]["part"]]),
    ):
        parent = finding["parent_assembly"]
        for child in children:
            for candidate in bom.descendants_of(parent):
                if candidate == child:
                    continue
                # An intermediate is a part that sits on the path: it is a descendant of the
                # parent and an ancestor of the planted child.
                if child in bom.descendants_of(candidate):
                    targets.add(candidate)
    return frozenset(targets)


@dataclass
class ReceiptEvent:
    part_id: str
    warehouse_id: str
    supplier_id: str | None
    order_day: int
    arrival_day: int
    quantity: int
    lead_days: int


@dataclass
class PairHistory:
    params: PairParams
    issues: np.ndarray
    receipts: np.ndarray
    on_hand: np.ndarray
    events: list[ReceiptEvent]
    opening_qty: int

    @property
    def closing_qty(self) -> int:
        return int(self.on_hand[-1])

    @property
    def in_transit_qty(self) -> int:
        """Ordered but not yet arrived as of the final day — feeds `on_order` for the risk model."""
        n = len(self.issues)
        return sum(e.quantity for e in self.events if e.arrival_day >= n)


def _sampled_lead_days(
    params: PairParams, supplier_id: str | None, order_index: int, days_ago: float
) -> int:
    """Draw an actual lead time from the supplier's TRUE archetype distribution.

    The contracted figure is what the master data says; this is what happens. The gap between
    them is nuance 5, and the spread is what nuance 6 prices. `days_ago` matters because the
    `improving` archetype's mean changes over time.
    """
    contracted = demand._lead_days_for(params.part_id)
    if supplier_id is None:
        return contracted
    mu, sigma = contracts.true_lead_parameters_at(supplier_id, contracted, days_ago)
    rng = demand._rng(params.part_id, params.warehouse_id, "lead", order_index)
    return max(1, round(rng.normal(mu, sigma)))


def _order_quantity(params: PairParams, on_hand_at_order: int, in_transit: int) -> int:
    """Order up to the target level, respecting MOQ and pack size."""
    gap = max(params.max_stock - on_hand_at_order - in_transit, 1)
    supplier_id = contracts.preferred_supplier(params.part_id)
    if supplier_id is None:
        return int(gap)
    row = contracts.contract_for(params.part_id, supplier_id)
    if row is None:
        return int(gap)
    moq, pack = row["moq"], row["pack_size"]
    rounded = int(np.ceil(gap / pack) * pack)
    return max(moq, rounded)


def simulate(params: PairParams) -> PairHistory:
    """Run the reorder loop, then steer the closing balance to the pair's finding target."""
    issues = demand.issue_series(params)
    n = len(issues)
    receipts = np.zeros(n, dtype=int)
    events: list[ReceiptEvent] = []
    supplier_id = contracts.preferred_supplier(params.part_id)

    opening = params.max_stock
    on_hand = opening
    pending: list[tuple[int, int]] = []  # (arrival_day, qty)
    # Standard reorder point: cover the lead-time demand, plus the safety buffer. Triggering off
    # safety stock alone guarantees a breach every cycle, because nothing covers the wait.
    reorder_point = params.safety_stock + params.effective_level * demand._lead_days_for(
        params.part_id
    )
    order_index = 0

    for day in range(n):
        for arrival_day, qty in [p for p in pending if p[0] == day]:
            on_hand += qty
            receipts[day] += qty
        pending = [p for p in pending if p[0] != day]

        # Deliberately NOT clamped at zero. The loop's view of on-hand has to match the
        # reconstruction (`opening - cumsum(issues) + cumsum(receipts)`), or its ordering
        # decisions are made against a balance that is systematically too high and it
        # under-orders. That divergence put 217 of 278 pairs' closing balance below safety stock,
        # where a corrective pin then stacked them all on exactly the same value.
        on_hand -= int(issues[day])

        in_transit = sum(q for _, q in pending)
        if on_hand + in_transit < reorder_point:
            qty = _order_quantity(params, on_hand, in_transit)
            lead = _sampled_lead_days(params, supplier_id, order_index, days_ago=n - 1 - day)
            arrival = day + lead
            pending.append((arrival, qty))
            events.append(
                ReceiptEvent(
                    part_id=params.part_id,
                    warehouse_id=params.warehouse_id,
                    supplier_id=supplier_id,
                    order_day=day,
                    arrival_day=arrival,
                    quantity=qty,
                    lead_days=lead,
                )
            )
            order_index += 1

    target = finding_target(params)
    if target is not None:
        # Solve for the opening balance rather than editing the tail. Since
        # closing = opening - total_issues + total_receipts, setting
        # opening = target + total_issues - total_receipts lands the closing balance exactly on
        # target, with no adjustment to any receipt.
        #
        # An earlier version steered by shrinking recent receipts and then lifted the whole
        # series when it went negative -- which moved the closing balance back off target, and
        # was the reason six of eight planted conditions silently missed.
        opening = int(target + issues.sum() - receipts.sum())
    else:
        # No terminal condition to preserve. Only step in if the balance would be physically
        # impossible, and add a varied buffer rather than pinning to a single value -- pinning
        # every corrected pair to exactly `safety_stock` produced a degenerate closing
        # distribution (p25 = p50 = p75) that no real inventory ever looks like.
        closing = opening - int(issues.sum()) + int(receipts.sum())
        if closing < 0:
            rng = demand._rng(params.part_id, params.warehouse_id, "opening")
            buffer_days = float(rng.uniform(4, 30))
            opening += -closing + round(params.effective_level * buffer_days)

    receipts = _lift_troughs(issues, receipts, opening)
    _sync_events_to_receipts(events, receipts, n)

    if suppresses_inbound(params):
        # Drop orders that would still be in transit today. Availability is
        # `on_hand + on_order`, so a pair with a large inbound quantity is not actually short
        # however low its on-hand balance is -- which silently defeated F2's planted buildable
        # level (1200 on hand, ~1400 inbound, so 2626 available and nothing bound).
        #
        # It is also the more honest construction: a part that is genuinely blocking production
        # is one that is short AND has nothing coming. Short-with-an-order-inbound is a waiting
        # problem, not a shortage.
        events = [e for e in events if e.arrival_day < n]

    series = opening - np.cumsum(issues) + np.cumsum(receipts)

    # Non-negativity is the property that can actually break -- reconciliation cannot, since the
    # series is defined as the running balance. If this trips, `_lift_troughs` ran out of later
    # receipts to draw forward.
    if series.min() < 0:
        raise AssertionError(
            f"{params.part_id}@{params.warehouse_id}: stock goes to {int(series.min())} "
            f"on day {int(series.argmin())} of {n}"
        )

    return PairHistory(
        params=params,
        issues=issues,
        receipts=receipts,
        on_hand=series.astype(int),
        events=events,
        opening_qty=int(opening),
    )


def _lift_troughs(issues: np.ndarray, receipts: np.ndarray, opening: int) -> np.ndarray:
    """Remove negative balances by moving receipt mass earlier, without changing the closing one.

    `series[t] = opening - CI[t] + CR[t]`. Moving quantity X from a receipt after the trough to a
    receipt before it raises `CR[t]` across the trough but leaves `CR[n-1]` — and therefore the
    closing balance — untouched. So the physical constraint is satisfiable without giving up the
    exact terminal condition the finding needs.

    Always terminates: if there were no receipt after the trough, the series would decline
    monotonically from it and the closing balance would itself be negative, which the caller's
    non-negative target rules out.
    """
    receipts = receipts.copy()
    n = len(receipts)

    for _ in range(_MAX_TROUGH_PASSES):
        series = opening - np.cumsum(issues) + np.cumsum(receipts)
        trough = int(series.argmin())
        if series[trough] >= 0:
            return receipts

        need = int(-series[trough])
        donors = [d for d in range(trough + 1, n) if receipts[d] > 0]
        if not donors:
            break

        for day in donors:
            take = min(need, int(receipts[day]))
            receipts[day] -= take
            receipts[0] += take
            need -= take
            if need == 0:
                break

    return receipts


_MAX_TROUGH_PASSES = 50


def _sync_events_to_receipts(events: list[ReceiptEvent], receipts: np.ndarray, n: int) -> None:
    """Keep the event log consistent with the steered receipt series.

    Without this, `fact_supplier_delivery` would record quantities that do not match the stock
    movements they supposedly caused — and the two facts are meant to describe one world.
    """
    for event in events:
        if event.arrival_day < n:
            event.quantity = int(receipts[event.arrival_day]) or event.quantity


# ---------------------------------------------------------------------------
# Finding targets — the exact closing balance each planted condition needs
# ---------------------------------------------------------------------------


def suppresses_inbound(params: PairParams) -> bool:
    """Whether this pair should have nothing in transit today.

    True for every pair planted as *short*: availability is on-hand plus on-order, so a short
    balance with a big inbound quantity is not a detectable shortage.
    """
    f = FINDINGS
    key = (params.part_id, params.warehouse_id)
    f11 = f["F11"]

    if params.part_id == f["F1"]["part"] and params.warehouse_id == f["F1"]["receiver"]:
        return True
    if params.part_id in f["F2"]["binding_children"] and params.warehouse_id == f["F2"]["warehouse"]:
        return True
    if key == (f["F3"]["part"], f["F3"]["warehouse"]):
        return True
    if key == (f["F7"]["part"], f["F7"]["warehouse"]):
        return True
    return key in (
        (f11["high_exposure_part"], f11["high_exposure_warehouse"]),
        (f11["transfer_fixable_part"], f11["transfer_fixable_warehouse"]),
    )


def finding_target(params: PairParams) -> int | None:
    """Closing on-hand this pair must land on, or None to let the loop end wherever it ends."""
    key = (params.part_id, params.warehouse_id)
    f = FINDINGS
    burn = max(params.effective_level, 0.1)

    # F8: heavily overstocked and no longer moving -- 400 days of cover at its historical rate.
    if key in {tuple(p) for p in f["F8"]["pairs"]}:
        return round(params.level * f["F8"]["cover_days"])

    # F1: a receiver genuinely short, a real donor with deep cover, and a decoy holding MORE
    # units but thinner cover. A detector ranking donors by quantity picks the decoy.
    f1 = f["F1"]
    if params.part_id == f1["part"]:
        if params.warehouse_id == f1["receiver"]:
            return round(params.safety_stock * 0.45)
        if params.warehouse_id == f1["best_donor"]:
            return round(burn * F1_BEST_DONOR_COVER_DAYS)
        if params.warehouse_id == f1["decoy_donor"]:
            # Fewer days of cover than the real donor, but the decoy's own burn is high enough
            # that this is MORE absolute units. A donor ranked on quantity picks this one; a
            # donor ranked on protected cover does not.
            return round(burn * F1_DECOY_COVER_DAYS)
        return round(burn * 35)

    # F3: below its own safety stock AND binding a parent build -- the case the current
    # implementation excludes from its cascade join and prices at the cost of the parts.
    if params.part_id == f["F3"]["part"] and params.warehouse_id == f["F3"]["warehouse"]:
        return round(params.safety_stock * 0.55)

    # The cascade parents themselves must hold little of their own stock. Supply is
    # `parent_on_hand + buildable_from_children`, so an assembly sitting on 1785 units absorbs
    # the whole shortfall and its children stop binding however short they are -- which silently
    # disarmed both cascade findings once the parent's own stock was (correctly) counted.
    cascade_parents = {f["F2"]["parent_assembly"], f["F3"]["parent_assembly"]}
    if params.part_id in cascade_parents and params.warehouse_id == f["F2"]["warehouse"]:
        return CASCADE_PARENT_ON_HAND

    # Sub-assemblies BETWEEN a cascade parent and its planted binding components must also be
    # held low. Supply recurses as `own_stock + buildable_from_children`, so a sub-assembly
    # sitting on ~1000 units supplies the parent by itself and the components below it stop
    # constraining anything -- which disarmed both cascade findings a second time, after the
    # parent's own stock had already been capped for the same reason.
    if (
        params.part_id in _cascade_intermediates()
        and params.warehouse_id == f["F2"]["warehouse"]
    ):
        return CASCADE_INTERMEDIATE_ON_HAND

    # F2: two children of one parent that bind at the SAME buildable level, so fixing either
    # one alone leaves the parent blocked. Co-binding is a property of
    # `available / multiplier_to_parent`, not of raw quantity -- giving both children the same
    # stock only co-binds while both multipliers happen to be 1.
    if params.part_id in f["F2"]["binding_children"] and params.warehouse_id == f["F2"]["warehouse"]:
        multiplier = bom.multiplier_to_ancestor(params.part_id, f["F2"]["parent_assembly"]) or 1
        return F2_BUILDABLE_UNITS * multiplier

    # F6: leave a replenishment gap near 60 units so MOQ 500 forces a ~7-month overbuy.
    if params.part_id == f["F6"]["part"]:
        supplier_id = contracts.preferred_supplier(params.part_id)
        lead = contracts.contracted_lead_days(params.part_id, supplier_id) or 30
        _, sigma = contracts.true_lead_parameters(supplier_id, lead)
        cover = policy.target_cover_days(lead, sigma, params.criticality_class)
        return max(1, round(cover * burn - 60))

    # F7: short enough that the under-sized buffer actually bites.
    if params.part_id == f["F7"]["part"] and params.warehouse_id == f["F7"]["warehouse"]:
        return round(params.safety_stock * 0.8)

    # F11: two exposures whose ranking should invert once cost-to-fix is accounted for.
    f11 = f["F11"]
    if key == (f11["high_exposure_part"], f11["high_exposure_warehouse"]):
        return round(params.safety_stock * 0.3)
    if params.part_id == f11["high_exposure_part"]:
        # Peer warehouses sit exactly ON their safety stock: not short themselves, but with
        # nothing donatable. That is what makes this exposure buy-only, and a long-lead buy is
        # what pushes its decision value below the smaller transfer-fixable exposure.
        return params.safety_stock
    if key == (f11["transfer_fixable_part"], f11["transfer_fixable_warehouse"]):
        return round(params.safety_stock * 0.4)
    if key == (f11["transfer_fixable_part"], f11["transfer_fixable_donor"]):
        return round(burn * 90)

    return None


def simulate_all() -> dict[tuple[str, str], PairHistory]:
    """Every pair's full history. The expensive call — a few seconds for ~278 pairs."""
    return {key: simulate(params) for key, params in demand.pair_params().items()}
