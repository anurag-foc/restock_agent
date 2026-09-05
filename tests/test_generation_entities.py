"""Tests for the deterministic dataset generators.

These modules are pure Python by design (no Spark, no Databricks) precisely so this file can
exist -- docs/dataset_generator_spec.md §5 requires determinism and cohort coverage, and both
are cheap to assert here rather than discovering a broken dataset after a 500K-row load.
"""

from collections import Counter

import pytest

from agentic_restock.generation import bom, entities, policy

# --- policy -----------------------------------------------------------------


def test_holding_rate_is_sum_of_its_named_components():
    assert policy.HOLDING_RATE == pytest.approx(0.14)
    assert sum(policy.HOLDING_RATE_COMPONENTS.values()) == pytest.approx(policy.HOLDING_RATE)


def test_holding_rate_is_not_the_uncited_industry_figure():
    # docs/market_evidence_phase1.md §8 traces 20-30% to a 1995 trade article. Being anywhere
    # in that band means someone reverted to the default this project explicitly rejected.
    assert policy.HOLDING_RATE < 0.20


def test_criticality_normalisation_handles_both_real_spellings():
    # dim_part carries 'A-CRITICAL' and 'A - CRITICAL' for the same class. An exact match drops
    # half the safety-critical parts to the default tier, silently giving them 95% instead of 99%.
    assert policy.z_for("A-CRITICAL") == policy.z_for("A - CRITICAL")
    assert policy.service_level_for("A - CRITICAL") == 0.99


def test_unknown_criticality_falls_back_rather_than_raising():
    assert policy.z_for(None) == policy.Z_BY_CLASS[policy.DEFAULT_CRITICALITY_CLASS]
    assert policy.z_for("MYSTERY") == policy.Z_BY_CLASS[policy.DEFAULT_CRITICALITY_CLASS]


def test_erratic_supplier_forces_more_cover_than_a_reliable_one():
    """The property that makes FX2's variance premium legible: same mean, different spread."""
    reliable = policy.target_cover_days(mu_lead=40, sigma_lead=5, criticality_class="B")
    erratic = policy.target_cover_days(mu_lead=40, sigma_lead=14, criticality_class="B")
    assert erratic > reliable
    assert erratic - reliable == pytest.approx(policy.Z_BY_CLASS["B"] * 9)


def test_target_cover_includes_the_review_period():
    assert policy.target_cover_days(30, 0, "B") == pytest.approx(30 + policy.REVIEW_PERIOD_DAYS)


def test_excess_holding_cost_scales_with_how_long_the_excess_sits():
    """The superseded flat 2% ignored time, understating a multi-month overbuy 4-5x."""
    fast = policy.excess_holding_cost(excess_qty=100, unit_cost=1000, daily_burn=10)
    slow = policy.excess_holding_cost(excess_qty=100, unit_cost=1000, daily_burn=1)
    assert slow == pytest.approx(fast * 10)
    assert policy.excess_holding_cost(0, 1000, 10) == 0.0
    assert policy.excess_holding_cost(100, 1000, 0) == 0.0


# --- entities ---------------------------------------------------------------


def test_generation_is_deterministic():
    assert entities.parts() == entities.parts()
    assert entities.warehouses() == entities.warehouses()
    assert entities.suppliers() == entities.suppliers()
    assert bom.part_bom() == bom.part_bom()


def test_part_counts_by_bom_level():
    levels = Counter(p["BOM_LEVEL"] for p in entities.parts())
    assert levels == {0: 12, 1: 28, 2: 60}


def test_part_ids_and_keys_are_unique():
    parts = entities.parts()
    assert len({p["PART_ID"] for p in parts}) == len(parts)
    assert len({p["PART_KEY"] for p in parts}) == len(parts)


def test_unit_costs_span_orders_of_magnitude():
    """A dataset where every part costs the same makes exposure ranking look like qty ranking."""
    costs = [p["UNIT_COST"] for p in entities.parts()]
    assert max(costs) / min(costs) > 1000


def test_both_criticality_spellings_are_present():
    """Deliberate: keeps the board's REPLACE(...) normalisation exercised (spec §7.2)."""
    classes = Counter(p["CRITICALITY_CLASS"] for p in entities.parts())
    assert classes["A-CRITICAL"] > 0
    assert classes["A - CRITICAL"] > 0


def test_warehouses_include_non_active_members_for_filter_coverage():
    statuses = Counter(w["OPERATIONAL_STATUS"] for w in entities.warehouses())
    assert statuses["ACTIVE"] == 8
    assert sum(v for k, v in statuses.items() if k != "ACTIVE") == 2


def test_every_supplier_archetype_has_enough_members_to_not_be_dead_code():
    counts = Counter(s["_archetype"] for s in entities.suppliers())
    assert set(counts) == set(entities.SUPPLIER_ARCHETYPES)
    assert all(n >= 3 for n in counts.values()), counts


def test_the_loose_archetype_has_zero_drift_but_high_spread():
    """F4's whole point: on contract on average, unmanageable in practice."""
    offset, sigma, _, _ = entities.SUPPLIER_ARCHETYPES["loose"]
    tight_offset, tight_sigma, _, _ = entities.SUPPLIER_ARCHETYPES["tight"]
    assert offset == 0.0  # exactly on contract, so mean drift is genuinely zero
    assert sigma > tight_sigma * 5  # but wildly inconsistent
    # A reliable supplier must be *better* than on-contract, not merely equal to it: a symmetric
    # distribution centred on the promise date is late half the time by definition.
    assert tight_offset < 0


def test_the_supply_base_is_mostly_reliable():
    """If most suppliers misbehave, "this supplier is a problem" carries no information."""
    grouped = entities.suppliers_by_archetype()
    reliable = len(grouped["tight"])
    total = sum(len(v) for v in grouped.values())
    assert reliable / total >= 0.3


def test_archetype_lookup_is_stable_against_mix_changes():
    """The catalog resolves suppliers this way so rebalancing counts cannot invert a finding."""
    for archetype in entities.SUPPLIER_ARCHETYPES:
        resolved = entities.supplier_with_archetype(archetype)
        assert entities.supplier_archetype_map()[resolved] == archetype


# --- bom --------------------------------------------------------------------


def test_bom_has_genuine_multi_level_links():
    """The replica's 5 pre-existing rows were flat; a recursive explosion needs real depth."""
    rows = bom.part_bom()
    parents = {r["fg_part_id"] for r in rows}
    intermediate = [r for r in rows if r["component_part_id"] in parents]
    assert len(intermediate) == 28


def test_every_parent_has_at_least_two_children():
    """Room to construct co-binding children (F2) requires >=2 children per parent."""
    kids = Counter(r["fg_part_id"] for r in bom.part_bom())
    assert min(kids.values()) >= 2


def test_descendants_reach_the_component_level():
    descendants = bom.descendants_of("P0001")
    parts_by_id = {p["PART_ID"]: p for p in entities.parts()}
    levels = {parts_by_id[p]["BOM_LEVEL"] for p in descendants}
    assert levels == {1, 2}


def test_every_assembly_is_consumed_by_at_least_one_model():
    assemblies = [p["PART_ID"] for p in entities.parts() if p["BOM_LEVEL"] == 0]
    for assembly in assemblies:
        assert bom.models_using(assembly), assembly


def test_some_assemblies_are_multi_per_vehicle():
    """So cascade arithmetic multiplies rather than passing a quantity straight through."""
    qtys = {r["QTY_PER_VEHICLE"] for r in bom.model_bom()}
    assert qtys - {1}


def test_qty_per_unit_lookup_rejects_a_non_child():
    with pytest.raises(KeyError):
        bom.qty_per_unit("P0001", "P0099")
