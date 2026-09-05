"""Tests for the reorder loop and the planted terminal conditions.

The full simulation is ~10s, so it is built once per session in conftest.py and shared.

Two of these guard bugs found during the build:

- `test_no_pair_holds_negative_stock` — the loop clamped on-hand at zero on a stockout while the
  reconstruction did not, so every historical stockout reconstructed as negative stock. 200 of
  278 pairs were affected.
- `test_every_planted_target_is_hit_exactly` — steering by shrinking recent receipts, then
  lifting the series when it went negative, moved the closing balance back off target. Six of
  eight planted conditions silently missed.
"""

import numpy as np
import pytest

from agentic_restock.generation import demand, replenishment, scenarios

# --- structural invariants --------------------------------------------------


def test_reconciliation_holds_for_every_pair_and_day(histories):
    """on_hand[t] == on_hand[t-1] - issues[t] + receipts[t], the auditability property."""
    for (part_id, warehouse_id), history in histories.items():
        expected = (
            history.opening_qty - np.cumsum(history.issues) + np.cumsum(history.receipts)
        ).astype(int)
        assert np.array_equal(history.on_hand, expected), f"{part_id}@{warehouse_id}"


def test_no_pair_holds_negative_stock(histories):
    for (part_id, warehouse_id), history in histories.items():
        assert history.on_hand.min() >= 0, f"{part_id}@{warehouse_id}"


def test_every_planted_target_is_hit_exactly(histories):
    for key, history in histories.items():
        target = replenishment.finding_target(history.params)
        if target is not None:
            assert history.closing_qty == target, key


def test_lifting_troughs_preserves_the_closing_balance():
    """The property that lets a physical constraint and an exact target coexist."""
    issues = np.array([0, 30, 30, 30, 30, 30, 30], dtype=int)
    receipts = np.array([0, 0, 0, 0, 0, 0, 120], dtype=int)
    opening = 60
    before = opening - issues.sum() + receipts.sum()
    lifted = replenishment._lift_troughs(issues, receipts, opening)
    after = opening - issues.sum() + lifted.sum()
    assert after == before
    assert (opening - np.cumsum(issues) + np.cumsum(lifted)).min() >= 0


def test_simulation_is_deterministic():
    params = demand.pair_params()[("P0045", "WH005")]
    first = replenishment.simulate(params)
    second = replenishment.simulate(params)
    assert np.array_equal(first.on_hand, second.on_hand)
    assert first.opening_qty == second.opening_qty
    assert [e.quantity for e in first.events] == [e.quantity for e in second.events]


def test_receipt_events_exist_and_carry_a_sampled_lead_time(histories):
    events = [e for h in histories.values() for e in h.events]
    assert len(events) > 1000
    assert all(e.lead_days >= 1 for e in events)
    assert all(e.arrival_day > e.order_day for e in events)
    # Actual lead times must vary, or nuance 5 has nothing to detect.
    assert len({e.lead_days for e in events}) > 20


def test_some_stock_is_in_transit_today(histories):
    """`on_order_arriving_within_lead` is an input to the risk model; it cannot be all zeros."""
    assert sum(1 for h in histories.values() if h.in_transit_qty > 0) > 20


# --- planted conditions -----------------------------------------------------


def test_f1_decoy_donor_holds_more_units_but_less_cover(histories):
    """The trap: ranking donors by quantity picks the decoy; ranking by cover does not."""
    f1 = scenarios.FINDINGS["F1"]
    best = histories[(f1["part"], f1["best_donor"])]
    decoy = histories[(f1["part"], f1["decoy_donor"])]
    assert decoy.closing_qty > best.closing_qty
    best_cover = best.closing_qty / best.params.effective_level
    decoy_cover = decoy.closing_qty / decoy.params.effective_level
    assert decoy_cover < best_cover


def test_f1_receiver_is_genuinely_short(histories):
    f1 = scenarios.FINDINGS["F1"]
    receiver = histories[(f1["part"], f1["receiver"])]
    assert receiver.closing_qty < receiver.params.safety_stock


def test_f2_children_are_short_and_bind_at_the_same_buildable_level(histories):
    """Co-binding: two children equally constraining, so fixing one leaves the parent blocked.

    Measured in *buildable parent units* (`available / multiplier_to_parent`), not in days of
    cover. Days of cover differ because the two children have different burn rates, and an
    earlier version of this test compared cover and so demanded a coincidence rather than the
    property that matters.
    """
    from agentic_restock.generation import bom

    f2 = scenarios.FINDINGS["F2"]
    buildable = []
    for child in f2["binding_children"]:
        history = histories[(child, f2["warehouse"])]
        assert history.closing_qty < history.params.safety_stock
        assert history.in_transit_qty == 0  # short with nothing inbound
        multiplier = bom.multiplier_to_ancestor(child, f2["parent_assembly"]) or 1
        buildable.append(history.closing_qty // multiplier)
    assert len(set(buildable)) == 1, buildable


def test_f3_part_is_below_safety_stock(histories):
    """The case the current cascade join excludes, and so prices at the cost of the parts."""
    f3 = scenarios.FINDINGS["F3"]
    history = histories[(f3["part"], f3["warehouse"])]
    assert history.closing_qty < history.params.safety_stock


def test_f6_leaves_a_replenishment_gap_that_moq_dwarfs(histories):
    """~60 units needed against MOQ 500 — a ~7-month overbuy, not an abstract constraint."""
    from agentic_restock.generation import contracts

    f6 = scenarios.FINDINGS["F6"]
    history = next(h for k, h in histories.items() if k[0] == f6["part"])
    gap = history.params.max_stock - history.closing_qty
    assert 40 <= gap <= 90, gap

    contract = next(r for r in contracts.contracts() if r["part_id"] == f6["part"])
    assert contract["moq"] >= 500
    excess = contract["moq"] - gap
    months = excess / history.params.effective_level / 30
    assert months > 5, months


def test_f8_pairs_hold_roughly_a_year_of_dead_cover(histories):
    f8 = scenarios.FINDINGS["F8"]
    for part_id, warehouse_id in f8["pairs"]:
        history = histories[(part_id, warehouse_id)]
        cover = history.closing_qty / history.params.level
        assert cover == pytest.approx(f8["cover_days"], rel=0.02)
        assert history.issues[history.params.quiet_since_day :].sum() == 0


def test_f11_transfer_fixable_side_has_a_donor_with_surplus(histories):
    """Without a real donor the transfer fix does not exist and the inversion cannot happen."""
    f11 = scenarios.FINDINGS["F11"]
    short = histories[(f11["transfer_fixable_part"], f11["transfer_fixable_warehouse"])]
    donor = histories[(f11["transfer_fixable_part"], f11["transfer_fixable_donor"])]
    assert short.closing_qty < short.params.safety_stock
    assert donor.closing_qty > donor.params.safety_stock * 2


def test_f11_buy_only_side_has_no_network_donor(histories):
    """Its only fix must be a long-lead purchase, which is what depresses its decision value."""
    f11 = scenarios.FINDINGS["F11"]
    part = f11["high_exposure_part"]

    short = histories[(part, f11["high_exposure_warehouse"])]
    assert short.closing_qty < short.params.safety_stock

    # No peer warehouse may hold stock it could give up without breaching its own safety stock,
    # or a transfer would be available and the inversion would not be a buy-only case.
    peers = [
        h
        for (peer_part, peer_wh), h in histories.items()
        if peer_part == part and peer_wh != f11["high_exposure_warehouse"]
    ]
    assert peers, "the buy-only part must still be stocked elsewhere for this to mean anything"
    for peer in peers:
        donatable = peer.closing_qty - peer.params.safety_stock
        assert donatable <= 0, f"{peer.params.warehouse_id} could donate {donatable}"


def test_f11_lead_time_is_long_enough_to_make_buying_expensive(histories):
    from agentic_restock.generation import contracts

    f11 = scenarios.FINDINGS["F11"]
    lead = contracts.contracted_lead_days(
        f11["high_exposure_part"], f11["high_exposure_supplier"]
    )
    assert lead == f11["high_exposure_lead_days"]
    assert lead >= 60
