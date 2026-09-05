"""Phase 4's gate: the fix constructors and the eight scanners.

Guards bugs found during the build, all of which produced plausible output rather than failing:

- **The variance premium was a constant.** It was divided by the buffer it was pricing, which
  cancels `sigma_lead` and leaves `holding_rate x unit_cost` — identical for every supplier, so
  it contributed nothing to the ranking and the reversal it exists to produce could not happen.
- **`action_cost` was zero on the largest findings**, because in-house assemblies have no
  contract and S1 scanned them anyway. Eight assemblies sat at the top of the ranking with
  decision value equal to exposure.
- **The consequence proxy scaled with holdings**, so holding more stock increased what it cost
  to run out — ₹218 crore for a single part.
- **The cascade fix was unpriced**, which held `decision_value == exposure` for the biggest
  findings in the set.
"""

import pytest

from agentic_restock.detectors import findings as F
from agentic_restock.detectors import fixes
from agentic_restock.generation import policy

# --- FX1: transfer ---------------------------------------------------------


def _location(warehouse_id, **overrides):
    base = {
        "warehouse_id": warehouse_id,
        "available_qty": 1000,
        "safety_stock_qty": 200,
        "forward_burn": 10.0,
        "sigma_d": 2.0,
        "mu_lead": 40.0,
        "sigma_lead": 3.0,
        "consequence": 1_000_000.0,
    }
    base.update(overrides)
    return base


def test_transfer_needs_a_receiver_with_an_actual_shortfall():
    receiver = _location("WH1", available_qty=100_000)
    assert fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[_location("WH2")], freight_cost=500.0
    ) == []


def test_a_donor_is_not_pushed_below_its_own_safety_stock():
    receiver = _location("WH1", available_qty=0)
    donor = _location("WH2", available_qty=200, safety_stock_qty=200)
    assert fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[donor], freight_cost=100.0
    ) == []


def test_the_best_donor_is_the_one_with_deepest_cover_not_most_units():
    """The trap the superseded design fell into: the largest pile is often the wrong donor,
    because a warehouse holding more units while burning faster has less protected surplus."""
    receiver = _location("WH1", available_qty=0)
    deep = _location("WH_DEEP", available_qty=1000, forward_burn=5.0)  # 200 days cover
    big = _location("WH_BIG", available_qty=1400, forward_burn=40.0)  # 35 days cover
    options = fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[deep, big], freight_cost=100.0
    )
    assert options
    assert options[0].donor_warehouse_id == "WH_DEEP"


def test_transfer_benefit_nets_off_the_donor_risk():
    """Moving a shortage is not a fix, so the donor's increased risk is subtracted."""
    receiver = _location("WH1", available_qty=0)
    donor = _location("WH2", available_qty=600, forward_burn=10.0)
    option = fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[donor], freight_cost=100.0
    )[0]
    naive = (option.receiver_risk_before - option.receiver_risk_after) * receiver["consequence"]
    assert option.benefit <= naive
    assert option.donor_risk_after >= option.donor_risk_before


def test_transfer_cost_is_freight_not_purchase_price():
    receiver = _location("WH1", available_qty=0)
    option = fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[_location("WH2")], freight_cost=1750.0
    )[0]
    assert option.action_cost == 1750.0


# --- FX2: supplier economics -----------------------------------------------


def test_the_variance_premium_actually_depends_on_the_spread():
    """It was divided by the buffer it priced, cancelling sigma_lead and leaving a constant."""
    tight, _ = fixes.effective_unit_cost(
        quoted_unit_cost=100.0, reject_rate=0.0, sigma_lead_days=1.0,
        forward_burn=10.0, unit_cost=100.0, criticality_class="B",
    )
    loose, _ = fixes.effective_unit_cost(
        quoted_unit_cost=100.0, reject_rate=0.0, sigma_lead_days=20.0,
        forward_burn=10.0, unit_cost=100.0, criticality_class="B",
    )
    assert loose > tight, "a wider spread must cost more"


def test_rejects_raise_the_effective_cost():
    clean, _ = fixes.effective_unit_cost(
        quoted_unit_cost=100.0, reject_rate=0.0, sigma_lead_days=2.0,
        forward_burn=10.0, unit_cost=100.0, criticality_class="B",
    )
    scrappy, _ = fixes.effective_unit_cost(
        quoted_unit_cost=100.0, reject_rate=0.10, sigma_lead_days=2.0,
        forward_burn=10.0, unit_cost=100.0, criticality_class="B",
    )
    assert scrappy == pytest.approx(clean + 10.0, rel=0.01)


def test_effective_cost_can_reverse_the_quoted_price_ranking():
    """F5's whole premise. A 0-100 reliability score is unarguable; a reversed cost ranking is
    not — and it only reverses when the price gap is small enough for quality to decide it."""
    ranked = fixes.rank_suppliers(
        part_id="P1",
        quotes=[
            {"supplier_id": "CHEAP_BAD", "quoted_unit_cost": 100.0, "reject_rate": 0.08,
             "sigma_lead_days": 14.0},
            {"supplier_id": "DEARER_GOOD", "quoted_unit_cost": 104.0, "reject_rate": 0.005,
             "sigma_lead_days": 2.0},
        ],
        forward_burn=10.0,
        unit_cost=100.0,
        criticality_class="B",
    )
    assert ranked[0].supplier_id == "DEARER_GOOD"
    assert ranked[0].quoted_unit_cost > ranked[1].quoted_unit_cost


# --- FX3: orderable quantity ----------------------------------------------


def test_the_order_quantity_is_computed_from_the_gap():
    """Letting a caller supply it is what disabled the MOQ constraint: the function passes
    through any quantity at or above MOQ with zero excess, so asking big made the constraint
    vanish. One live quote recommended 910 units against a target level of 188."""
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=50.0, forward_burn=2.0,
        available_qty=40, moq=10, pack_size=1, effective_unit_cost_per_unit=100.0,
        unit_cost=100.0, exposure=1_000_000.0,
    )
    assert option.required_qty == 60  # 50 x 2 - 40
    assert option.orderable_qty == 60
    assert option.excess_qty == 0


def test_moq_forces_an_overbuy_and_prices_it_over_time_to_consume():
    """The superseded flat 2% ignored duration and understated a multi-month overbuy 4-5x."""
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=50.0, forward_burn=2.0,
        available_qty=40, moq=500, pack_size=1, effective_unit_cost_per_unit=100.0,
        unit_cost=100.0, exposure=1_000_000.0,
    )
    assert option.required_qty == 60
    assert option.orderable_qty == 500
    assert option.excess_qty == 440
    assert option.excess_months == pytest.approx(440 / 2 / 30, rel=0.01)
    expected = policy.excess_holding_cost(440, 100.0, 2.0)
    assert option.excess_holding_cost == pytest.approx(expected)


def test_pack_size_rounds_up():
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=50.0, forward_burn=2.0,
        available_qty=0, moq=1, pack_size=30, effective_unit_cost_per_unit=10.0,
        unit_cost=10.0, exposure=100_000.0,
    )
    assert option.required_qty == 100
    assert option.orderable_qty == 120  # ceil(100/30) x 30


def test_an_uneconomic_moq_is_flagged_rather_than_silently_ordered():
    """Nuance 7 as a finding: the answer is to renegotiate the pack, not place the order."""
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=50.0, forward_burn=2.0,
        available_qty=40, moq=5000, pack_size=1, effective_unit_cost_per_unit=100.0,
        unit_cost=100.0, exposure=10_000.0,
    )
    assert option.is_uneconomic


def test_evidence_dumps_every_field_in_order():
    """A verbatim dump has no editorial latitude; a summary invites filling an optional slot
    from whatever number is nearest to hand."""
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=50.0, forward_burn=2.0,
        available_qty=40, moq=500, pack_size=1, effective_unit_cost_per_unit=100.0,
        unit_cost=100.0, exposure=1_000_000.0,
    )
    for key in (
        "target_cover_days", "forward_burn", "available_qty", "required_qty", "moq",
        "pack_size", "orderable_qty", "excess_qty", "excess_months", "excess_holding_cost",
        "effective_unit_cost", "subtotal", "action_cost",
    ):
        assert key in option.evidence, key


def test_nothing_needed_means_nothing_ordered():
    option = fixes.build_purchase_option(
        part_id="P1", supplier_id="S1", target_cover_days=10.0, forward_burn=1.0,
        available_qty=500, moq=100, pack_size=1, effective_unit_cost_per_unit=10.0,
        unit_cost=10.0, exposure=0.0,
    )
    assert option.required_qty == 0
    assert option.orderable_qty == 0
    assert option.action_cost == 0.0


# --- the finding shape -----------------------------------------------------


def test_decision_value_subtracts_a_real_cost():
    finding = F.Finding(
        finding_type=F.STOCKOUT_RISK, subject_type=F.SUBJECT_PART_WAREHOUSE,
        subject_id="P1@WH1", exposure=1_000_000.0, action_cost=250_000.0,
    )
    assert finding.decision_value == 750_000.0


def test_decision_value_floors_at_zero():
    """A fix costing more than the exposure is not worth negative money; it is not worth doing."""
    finding = F.Finding(
        finding_type=F.STOCKOUT_RISK, subject_type=F.SUBJECT_PART_WAREHOUSE,
        subject_id="P1@WH1", exposure=100.0, action_cost=5_000.0,
    )
    assert finding.decision_value == 0.0


def test_a_transfer_discloses_no_holding_rate():
    """The disclosure rule: only the assumptions THIS finding's numbers depend on. A transfer
    buys nothing and holds nothing extra, so a holding rate has no business on it."""
    disclosed = F.assumption_values([F.ASSUMPTION_SERVICE_LEVEL], criticality_class="B")
    assert F.ASSUMPTION_HOLDING_RATE not in disclosed
    assert F.ASSUMPTION_SERVICE_LEVEL in disclosed


def test_assumptions_are_labelled_as_policy_not_measurement():
    """A PM reading 14% needs to know a person chose it, not that it came from their data."""
    disclosed = F.assumption_values([F.ASSUMPTION_HOLDING_RATE])
    entry = disclosed[F.ASSUMPTION_HOLDING_RATE]
    assert entry["kind"] == "policy"
    assert entry["basis"]
    assert "14%" in entry["display"]


def test_the_consequence_proxy_is_labelled_as_an_approximation():
    """The weakest number in the system; it must never be dressed as measured."""
    disclosed = F.assumption_values([F.ASSUMPTION_CONSEQUENCE_PROXY])
    assert disclosed[F.ASSUMPTION_CONSEQUENCE_PROXY]["kind"] == "approximation"


def test_a_finding_row_carries_evidence_and_assumptions_as_json():
    finding = F.Finding(
        finding_type=F.MOQ_UNECONOMIC, subject_type=F.SUBJECT_SUPPLIER_PART,
        subject_id="P1/S1", exposure=1000.0,
        evidence={"moq": 500}, assumptions_used=F.assumption_values([F.ASSUMPTION_HOLDING_RATE]),
    )
    row = finding.to_row()
    assert '"moq": 500' in row["EVIDENCE_JSON"]
    assert "holding_rate" in row["ASSUMPTIONS_JSON"]
    assert row["DECISION_VALUE"] == 1000.0


def test_every_finding_type_has_a_distinct_name():
    assert len(set(F.ALL_FINDING_TYPES)) == len(F.ALL_FINDING_TYPES) == 8


def test_a_transfer_is_sized_to_the_service_level_not_to_mean_demand():
    """The first live run's worst finding, and 297 tests did not catch it.

    Sizing a transfer as `mean demand over lead time - available` tops the receiver up to the
    MEDIAN of the demand distribution, so residual stockout probability is pinned at ~0.50 by
    construction -- measured at 75 of 92 transfers on real data, while the same report printed
    `service_level = 99% (A-CRITICAL)` on its own ASSUMPTIONS line. The safety term is what makes
    the delivered service level match the promised one.
    """
    receiver = _location("WH1", available_qty=0, criticality_class="A-CRITICAL")
    donor = _location("WH2", available_qty=5000, criticality_class="A-CRITICAL")
    option = fixes.rank_transfer_options(
        part_id="P1", receiver=receiver, candidates=[donor], freight_cost=100.0
    )[0]

    target_miss_rate = 1.0 - policy.service_level_for("A-CRITICAL")
    assert option.receiver_risk_after <= target_miss_rate + 0.02
    # The regression itself: a fix that leaves a coin-flip is not a fix.
    assert option.receiver_risk_after < 0.4


def test_a_more_critical_part_is_topped_up_further():
    """The same z that prices an erratic supplier in FX2 must widen the ask here, or the two
    disagree about what 'covered' means for the same part."""
    donor = _location("WH2", available_qty=5000)
    critical = fixes.rank_transfer_options(
        part_id="P1",
        receiver=_location("WH1", available_qty=0, criticality_class="A-CRITICAL"),
        candidates=[donor],
        freight_cost=100.0,
    )[0]
    routine = fixes.rank_transfer_options(
        part_id="P1",
        receiver=_location("WH1", available_qty=0, criticality_class="C"),
        candidates=[donor],
        freight_cost=100.0,
    )[0]
    assert critical.transfer_qty > routine.transfer_qty


def test_an_erratic_lead_time_widens_the_transfer():
    """sigma_lead enters through sigma_DL, so an unpredictable supplier needs more on hand to
    reach the same service level -- the whole reason the variance term is not dropped."""
    donor = _location("WH2", available_qty=5000)
    steady = fixes.rank_transfer_options(
        part_id="P1",
        receiver=_location("WH1", available_qty=0, sigma_lead=1.0),
        candidates=[donor],
        freight_cost=100.0,
    )[0]
    erratic = fixes.rank_transfer_options(
        part_id="P1",
        receiver=_location("WH1", available_qty=0, sigma_lead=12.0),
        candidates=[donor],
        freight_cost=100.0,
    )[0]
    assert erratic.transfer_qty > steady.transfer_qty
