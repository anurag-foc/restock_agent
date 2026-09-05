"""Phase 3's gate: the consequence model and the risk model.

Guards four bugs found during the build, each of which silently disarmed a finding rather than
failing:

- **The cascade was flattened, not recursive.** Every descendant was pre-multiplied against the
  parent and a single global MIN taken, which treats a sub-assembly and its own components as
  independent constraints when they are additive for that branch. The sub-assembly was reported
  as the binding item and the components a PM would actually buy never surfaced.
- **Parent stock absorbed the block.** Supply is `parent_on_hand + buildable_from_children`, so
  an assembly holding 1785 units covered the whole shortfall and its children stopped binding.
- **Then the intermediate sub-assembly absorbed it**, for exactly the same reason, one level down.
- **Cascades were assessed at regional DCs**, which never build anything — 51 blocked parents
  worth ₹618 cr, almost all of it fictional.
"""

import math

import pytest

from agentic_restock.estimators import cascade, risk
from agentic_restock.generation import policy

# --- cascade: structure -----------------------------------------------------


def _edges(*triples) -> list[cascade.BomEdge]:
    return [cascade.BomEdge(p, c, q) for p, c, q in triples]


def test_multiplier_index_walks_multiple_levels():
    """A component two levels down consumes qty(parent->sub) x qty(sub->component)."""
    edges = _edges(("A", "S", 2.0), ("S", "C", 3.0))
    index = cascade.multiplier_index(edges)
    assert index[("A", "S")] == 2.0
    assert index[("A", "C")] == 6.0
    assert index[("S", "C")] == 3.0


def test_multiplier_index_tolerates_a_cyclic_bom():
    """Bad data, not a reason to recurse forever."""
    edges = _edges(("A", "B", 1.0), ("B", "A", 1.0))
    index = cascade.multiplier_index(edges)
    assert index  # terminates and returns something usable


def test_supply_is_additive_down_a_branch():
    """The recursion's whole point: a sub-assembly's own stock PLUS what its children can build.

    Flattened, this parent would be limited to the sub-assembly's 10 units. Recursively it is
    10 + 5 = 15, and the binding item is the component rather than the sub-assembly.
    """
    edges = _edges(("A", "S", 1.0), ("S", "C", 2.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=100,
        parent_on_hand=0,
        parent_unit_cost=1000.0,
        availability={"S": 10, "C": 10},
        edges=edges,
    )
    assert result is not None
    assert result.buildable_from_children == 15  # 10 on hand + floor(10/2)
    assert result.binding_children == ("C",)  # the actionable leaf, not the sub-assembly


def test_parent_own_stock_reduces_the_block():
    edges = _edges(("A", "C", 1.0))
    with_stock = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=100,
        parent_on_hand=40,
        parent_unit_cost=10.0,
        availability={"C": 10},
        edges=edges,
    )
    assert with_stock.supply_units == 50
    assert with_stock.units_blocked == 50


def test_co_binding_children_are_both_reported():
    """Fixing one leaves the parent blocked at the other, so the action is the whole set."""
    edges = _edges(("A", "X", 1.0), ("A", "Y", 1.0), ("A", "Z", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=100,
        parent_on_hand=0,
        parent_unit_cost=10.0,
        availability={"X": 20, "Y": 20, "Z": 90},
        edges=edges,
    )
    assert set(result.binding_children) == {"X", "Y"}
    assert result.units_blocked == 80


def test_a_child_not_stocked_here_does_not_constrain_the_build():
    """Treating a missing row as zero would make every parent look blocked by parts held
    elsewhere."""
    edges = _edges(("A", "X", 1.0), ("A", "ELSEWHERE", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=50,
        parent_on_hand=0,
        parent_unit_cost=10.0,
        availability={"X": 60},
        edges=edges,
    )
    assert result.units_blocked == 0
    assert "ELSEWHERE" not in result.child_buildable


def test_a_parent_with_no_bom_returns_nothing():
    assert (
        cascade.build_parent_cascade(
            parent_part_id="LEAF",
            warehouse_id="WH1",
            required_units=10,
            parent_on_hand=0,
            parent_unit_cost=1.0,
            availability={"LEAF": 0},
            edges=_edges(("A", "B", 1.0)),
        )
        is None
    )


def test_an_unblocked_parent_names_no_binding_children():
    edges = _edges(("A", "X", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=10,
        parent_on_hand=0,
        parent_unit_cost=10.0,
        availability={"X": 500},
        edges=edges,
    )
    assert result.units_blocked == 0
    assert result.binding_children == ()


# --- cascade: attribution ---------------------------------------------------


def test_value_at_risk_is_counted_once_per_parent():
    """Accumulating upward from each child made three components each claim the whole parent."""
    edges = _edges(("A", "X", 1.0), ("A", "Y", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=100,
        parent_on_hand=0,
        parent_unit_cost=1000.0,
        availability={"X": 10, "Y": 10},
        edges=edges,
    )
    assert len(result.binding_children) == 2
    assert cascade.total_value_at_risk([result]) == result.value_at_risk == 90_000.0


def test_each_binding_child_carries_the_full_consequence_and_names_the_others():
    """Attribution, not division: the full parent value is what is at stake if THIS child runs
    out, while the parent's value_at_risk is still only summed once."""
    edges = _edges(("A", "X", 1.0), ("A", "Y", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A",
        warehouse_id="WH1",
        required_units=100,
        parent_on_hand=0,
        parent_unit_cost=1000.0,
        availability={"X": 10, "Y": 10},
        edges=edges,
    )
    consequences = cascade.child_consequence([result])
    assert consequences[("X", "WH1")]["value_at_risk"] == 90_000.0
    assert consequences[("Y", "WH1")]["value_at_risk"] == 90_000.0
    assert consequences[("X", "WH1")]["co_binding_children"] == ("Y",)


def test_a_child_binding_two_parents_carries_the_larger_consequence():
    edges = _edges(("A", "X", 1.0), ("B", "X", 1.0))
    small = cascade.build_parent_cascade(
        parent_part_id="A", warehouse_id="WH1", required_units=100, parent_on_hand=0,
        parent_unit_cost=10.0, availability={"X": 10}, edges=edges,
    )
    large = cascade.build_parent_cascade(
        parent_part_id="B", warehouse_id="WH1", required_units=100, parent_on_hand=0,
        parent_unit_cost=5000.0, availability={"X": 10}, edges=edges,
    )
    consequences = cascade.child_consequence([small, large])
    assert consequences[("X", "WH1")]["threatened_parent_part_id"] == "B"


def test_an_unblocked_parent_contributes_no_consequence():
    edges = _edges(("A", "X", 1.0))
    result = cascade.build_parent_cascade(
        parent_part_id="A", warehouse_id="WH1", required_units=1, parent_on_hand=0,
        parent_unit_cost=10.0, availability={"X": 500}, edges=edges,
    )
    assert cascade.child_consequence([result]) == {}


# --- risk -------------------------------------------------------------------


def test_normal_cdf_is_exact_at_known_points():
    assert risk.normal_cdf(0.0) == pytest.approx(0.5)
    assert risk.normal_cdf(1.6449) == pytest.approx(0.95, abs=1e-4)
    assert risk.normal_cdf(-1.6449) == pytest.approx(0.05, abs=1e-4)
    assert risk.normal_cdf(8.0) == pytest.approx(1.0)


def test_lead_time_variance_contributes_to_demand_variance():
    """Dropping the sigma_lead term is the usual mistake, and it makes an erratic supplier look
    as safe as a consistent one."""
    _, tight = risk.demand_over_lead(forward_burn=10, sigma_d=2, mu_lead=40, sigma_lead=2)
    _, loose = risk.demand_over_lead(forward_burn=10, sigma_d=2, mu_lead=40, sigma_lead=14)
    assert loose > tight * 3


def test_mean_demand_over_lead_is_burn_times_lead():
    mu, _ = risk.demand_over_lead(forward_burn=10, sigma_d=1, mu_lead=40, sigma_lead=1)
    assert mu == pytest.approx(400)


def test_stockout_probability_rises_as_stock_falls():
    def probability(available):
        p, _, _ = risk.stockout_probability(
            forward_burn=10, sigma_d=3, mu_lead=40, sigma_lead=4, available_qty=available
        )
        return p

    assert probability(1000) < probability(400) < probability(50)
    assert probability(400) == pytest.approx(0.5, abs=0.05)  # at the mean


def test_a_part_that_is_not_moving_cannot_run_out():
    """Dead capital is a different finding, not a certain stockout."""
    p, _, _ = risk.stockout_probability(
        forward_burn=0.0, sigma_d=0.0, mu_lead=40, sigma_lead=4, available_qty=0
    )
    assert p == 0.0


def test_low_confidence_widens_the_distribution_toward_a_coin_flip():
    """The property that ranks poorly-understood parts with less conviction, without a rule."""
    confident, _, _ = risk.stockout_probability(
        forward_burn=10, sigma_d=1, mu_lead=40, sigma_lead=1, available_qty=200
    )
    uncertain, _, _ = risk.stockout_probability(
        forward_burn=10, sigma_d=12, mu_lead=40, sigma_lead=12, available_qty=200
    )
    assert confident > uncertain  # both short, but the uncertain one is pulled toward 0.5
    assert abs(uncertain - 0.5) < abs(confident - 0.5)


def test_planned_production_beats_the_criticality_proxy():
    measured, basis = risk.consequence_of_stockout(
        unit_cost=700, unserved_units=2822, criticality_class="B",
        cascade_value_at_risk=109_600_000.0,
    )
    assert basis == "PLANNED_PRODUCTION"
    assert measured == 109_600_000.0


def test_the_criticality_proxy_scales_with_class():
    critical, _ = risk.consequence_of_stockout(
        unit_cost=100, unserved_units=10, criticality_class="A-CRITICAL"
    )
    standard, _ = risk.consequence_of_stockout(
        unit_cost=100, unserved_units=10, criticality_class="C"
    )
    assert critical > standard * 4


def test_the_criticality_proxy_handles_both_real_spellings():
    clean, _ = risk.consequence_of_stockout(
        unit_cost=100, unserved_units=10, criticality_class="A-CRITICAL"
    )
    spaced, _ = risk.consequence_of_stockout(
        unit_cost=100, unserved_units=10, criticality_class="A - CRITICAL"
    )
    assert clean == spaced


def test_exposure_is_probability_times_consequence():
    assessment = risk.assess(
        forward_burn=10, sigma_d=3, burn_confidence="HIGH",
        mu_lead=40, sigma_lead=4, available_qty=100,
        unit_cost=700, safety_stock_qty=500, criticality_class="B",
        cascade_value_at_risk=10_000_000.0,
    )
    assert assessment.exposure == pytest.approx(
        assessment.p_stockout * assessment.consequence
    )
    assert not assessment.is_estimated_consequence


def test_a_proxy_consequence_is_flagged_as_estimated():
    """It is the weakest number in the system and must be labelled, not shown as measured."""
    assessment = risk.assess(
        forward_burn=10, sigma_d=3, burn_confidence="HIGH",
        mu_lead=40, sigma_lead=4, available_qty=100,
        unit_cost=700, safety_stock_qty=500, criticality_class="B",
    )
    assert assessment.is_estimated_consequence
    assert assessment.consequence_basis == "CRITICALITY_PROXY"


def test_probability_ranks_differently_from_raw_shortfall():
    """The reason for the change: a near-certain small loss should outrank a remote large one,
    and the superseded formula cannot express that at all."""
    likely_small = risk.assess(
        forward_burn=10, sigma_d=1, burn_confidence="HIGH", mu_lead=40, sigma_lead=1,
        available_qty=50, unit_cost=100, safety_stock_qty=60, criticality_class="B",
        cascade_value_at_risk=500_000.0,
    )
    unlikely_large = risk.assess(
        forward_burn=10, sigma_d=1, burn_confidence="HIGH", mu_lead=40, sigma_lead=1,
        available_qty=5000, unit_cost=100, safety_stock_qty=60, criticality_class="B",
        cascade_value_at_risk=50_000_000.0,
    )
    assert likely_small.p_stockout > 0.9
    assert unlikely_large.p_stockout < 0.1
    assert likely_small.exposure > unlikely_large.exposure

    # The superseded formula sees them as identical, because it ignores likelihood entirely.
    assert risk.shortfall_exposure(
        safety_stock_qty=60, available_qty=50, unit_cost=100
    ) == 1000.0
    assert risk.shortfall_exposure(
        safety_stock_qty=60, available_qty=5000, unit_cost=100
    ) == 0.0


def test_target_cover_uses_the_same_z_as_the_risk_model():
    """FX2's variance premium and the cover target must not become independently-tuned knobs."""
    for criticality in ("A-CRITICAL", "B", "C"):
        cover = policy.target_cover_days(40, 10, criticality)
        expected = 40 + policy.REVIEW_PERIOD_DAYS + policy.z_for(criticality) * 10
        assert cover == pytest.approx(expected)
        assert not math.isnan(cover)


def test_the_consequence_proxy_scales_with_flow_not_with_holdings():
    """Holding more stock cannot increase what it costs to run out. Scaling by safety stock
    produced a ₹218 crore consequence for a single part."""
    small_flow, _ = risk.consequence_of_stockout(
        unit_cost=1000, unserved_units=10, criticality_class="B"
    )
    large_flow, _ = risk.consequence_of_stockout(
        unit_cost=1000, unserved_units=100, criticality_class="B"
    )
    assert large_flow == pytest.approx(small_flow * 10)
