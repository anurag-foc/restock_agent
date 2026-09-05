"""Tests for the production plan, open POs, and decision history.

Guards bugs found during the build:

- `test_cascade_children_co_bind_at_the_same_level` — the two planted children only co-bound
  while both BOM multipliers happened to be 1; co-binding is a property of
  `available / multiplier`, not of raw quantity.
- `test_short_pairs_have_nothing_inbound` — availability is on-hand *plus* on-order, so the
  planted buildable level of 1200 was really 2626 once ~1400 in-transit units were counted, and
  nothing bound.
- `test_decision_history_is_spread_across_parts` — pairs sort by (part, warehouse), so taking a
  prefix put the entire decision history onto four parts.
- `test_every_resurfacing_commitment_sits_on_a_short_pair` — a stale request against a healthy
  part re-surfaces nothing.
"""

from collections import Counter

import pytest

from agentic_restock.generation import (
    bom,
    decisions,
    entities,
    procurement,
    production,
    scenarios,
)

# --- production plan --------------------------------------------------------


def test_planned_qty_is_populated_and_dates_are_real():
    """PLANNED_QTY is 0% populated and EXECUTION_DATE_KEY is -1 on every replica row today."""
    rows = production.execution_rows()
    assert rows
    assert all(row["PLANNED_QTY"] > 0 for row in rows)
    assert all(row["EXECUTION_DATE_KEY"] > 0 for row in rows)


def test_the_plan_extends_beyond_today():
    """A plan that stopped at today could only support 'what did we fail to build'."""
    rows = production.execution_rows()
    forward = [row for row in rows if row["ACTUAL_QTY"] == 0]
    assert len(forward) == production.PLAN_FORWARD_DAYS * len(entities.vehicle_models())


def test_actuals_miss_the_plan_slightly_more_often_than_they_beat_it():
    rows = [r for r in production.execution_rows() if r["ACTUAL_QTY"] > 0]
    attainment = sum(r["ACTUAL_QTY"] for r in rows) / sum(r["PLANNED_QTY"] for r in rows)
    assert 0.90 < attainment < 1.0


def test_the_model_mix_is_not_flat():
    """A flat plan makes every cascade identical and hides whether the plan is being read."""
    assert len(set(production.daily_plan().values())) > 3


def test_requirements_explode_through_all_three_bom_levels():
    requirements = production.part_requirements()
    parts_by_id = {p["PART_ID"]: p for p in entities.parts()}
    levels = {parts_by_id[part_id]["BOM_LEVEL"] for part_id in requirements}
    assert levels == {0, 1, 2}


def test_vehicle_builds_reference_only_regenerated_dimensions():
    """Renumbering model/plant/line keys orphans the pre-existing 1110 rows (spec §5.6)."""
    model_keys = {m["MODEL_KEY"] for m in entities.vehicle_models()}
    plant_keys = {p["PLANT_KEY"] for p in entities.plants()}
    line_keys = {line["LINE_KEY"] for line in entities.production_lines()}
    for row in production.vehicle_build_rows():
        assert row["MODEL_KEY"] in model_keys
        assert row["PLANT_KEY"] in plant_keys
        assert row["LINE_KEY"] in line_keys


# --- cascade findings, against the plan -------------------------------------


def _buildable(histories, parent: str, warehouse: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for child in bom.descendants_of(parent):
        history = histories.get((child, warehouse))
        if history is None:
            continue
        multiplier = bom.multiplier_to_ancestor(child, parent) or 1
        available = history.closing_qty + history.in_transit_qty
        out.append((available // multiplier, child))
    return sorted(out)


def test_cascade_children_co_bind_at_the_same_level(histories):
    """Fixing one child must leave the parent blocked at the other."""
    f2 = scenarios.FINDINGS["F2"]
    buildable = _buildable(histories, f2["parent_assembly"], f2["warehouse"])
    minimum = buildable[0][0]
    binding = {child for level, child in buildable if level == minimum}
    assert binding == set(f2["binding_children"])


def test_the_cascade_parent_is_genuinely_blocked(histories):
    f2 = scenarios.FINDINGS["F2"]
    requirements = production.part_requirements()
    buildable = _buildable(histories, f2["parent_assembly"], f2["warehouse"])[0][0]
    assert requirements[f2["parent_assembly"]] > buildable


def test_short_pairs_have_nothing_inbound(histories):
    """Availability is on-hand plus on-order; inbound stock is a wait, not a shortage."""
    from agentic_restock.generation import replenishment

    suppressed = [
        history
        for history in histories.values()
        if replenishment.suppresses_inbound(history.params)
    ]
    assert suppressed
    assert all(history.in_transit_qty == 0 for history in suppressed)


def test_f3_is_worth_far_more_as_production_value_than_as_parts(histories):
    """The underpricing this finding exists to expose."""
    f3 = scenarios.FINDINGS["F3"]
    costs = {p["PART_ID"]: float(p["UNIT_COST"]) for p in entities.parts()}
    requirements = production.part_requirements()

    history = histories[(f3["part"], f3["warehouse"])]
    buildable = _buildable(histories, f3["parent_assembly"], f3["warehouse"])[0][0]
    blocked = max(requirements[f3["parent_assembly"]] - buildable, 0)

    production_value = blocked * costs[f3["parent_assembly"]]
    parts_value = (history.params.safety_stock - history.closing_qty) * costs[f3["part"]]
    assert blocked > 0
    assert production_value > parts_value * 100


# --- open purchase orders ---------------------------------------------------


def test_open_pos_exist_and_carry_pending_quantity(histories):
    rows = procurement.open_po_rows(histories)
    assert rows
    assert all(row["PENDING_QTY"] > 0 for row in rows)
    assert all(row["RECEIVED_QTY"] == 0 for row in rows)
    assert all(row["STATUS"] == "ISSUED" for row in rows)


def test_open_po_dates_are_real(histories):
    rows = procurement.open_po_rows(histories)
    assert all(row["ORDER_DATE_KEY"] > 0 for row in rows)
    assert all(row["EXPECTED_DATE_KEY"] > row["ORDER_DATE_KEY"] for row in rows)


def test_open_pos_match_the_in_transit_quantity_for_purchased_parts(histories):
    """An open PO and the in-transit stock the risk model sees are the same object.

    Scoped to purchased parts. Assemblies and sub-assemblies are built in-house and have no
    contract, so their inbound stock is a production transfer rather than a purchase order —
    comparing across everything mixes two different kinds of inbound.
    """
    from agentic_restock.generation import contracts

    rows = procurement.open_po_rows(histories)
    from_pos = sum(row["PENDING_QTY"] for row in rows)
    from_histories = sum(
        history.in_transit_qty
        for (part_id, _warehouse), history in histories.items()
        if contracts.preferred_supplier(part_id) is not None
    )
    assert from_pos == from_histories


def test_in_house_parts_have_inbound_stock_but_no_purchase_order(histories):
    """Documents the split rather than leaving it to be rediscovered as a mismatch."""
    from agentic_restock.generation import contracts

    in_house = [
        history
        for (part_id, _warehouse), history in histories.items()
        if contracts.preferred_supplier(part_id) is None
    ]
    assert in_house
    assert any(history.in_transit_qty > 0 for history in in_house)
    po_part_keys = {row["PART_KEY"] for row in procurement.open_po_rows(histories)}
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    for history in in_house:
        assert part_keys[history.params.part_id] not in po_part_keys


# --- decision history -------------------------------------------------------


def test_decision_plan_ages_match_their_labels():
    assert decisions.validate_plan() == []


def test_every_suppression_branch_is_represented(histories):
    statuses = Counter(
        row["_request_status"] for row in decisions.restock_request_rows(histories)
    )
    for required in (
        "PENDING_APPROVAL",
        "NEEDS_REVIEW",
        "APPROVED",
        "FULFILLING",
        "REJECTED",
        "COMPLETED",
    ):
        assert statuses[required] >= 1, required


def test_decision_history_is_spread_across_parts(histories):
    rows = decisions.restock_request_rows(histories)
    assert len({row["PART_KEY"] for row in rows}) >= len(rows) - 2


def test_no_decision_lands_on_a_finding_bearing_pair(histories):
    """A commitment there would suppress the finding it was planted to demonstrate."""
    part_ids = {p["PART_KEY"]: p["PART_ID"] for p in entities.parts()}
    warehouse_ids = {w["WAREHOUSE_KEY"]: w["WAREHOUSE_ID"] for w in entities.warehouses()}
    for row in decisions.restock_request_rows(histories):
        pair = (part_ids[row["PART_KEY"]], warehouse_ids[row["WAREHOUSE_KEY"]])
        assert not scenarios.expect_finding(*pair), pair


def test_every_resurfacing_commitment_sits_on_a_short_pair(histories):
    """A stale request against a healthy part re-surfaces nothing."""
    part_ids = {p["PART_KEY"]: p["PART_ID"] for p in entities.parts()}
    warehouse_ids = {w["WAREHOUSE_KEY"]: w["WAREHOUSE_ID"] for w in entities.warehouses()}
    resurfacing = [
        row
        for row in decisions.restock_request_rows(histories)
        if "re-surfaces" in row["_demonstrates"]
    ]
    assert len(resurfacing) == decisions.STALE_COMMITMENT_COUNT
    for row in resurfacing:
        pair = (part_ids[row["PART_KEY"]], warehouse_ids[row["WAREHOUSE_KEY"]])
        history = histories[pair]
        assert history.closing_qty < history.params.safety_stock, pair


def test_decided_lines_carry_a_note_and_undecided_ones_do_not(histories):
    for row in decisions.restock_request_rows(histories):
        if row["_request_status"] in ("PENDING_APPROVAL", "NEEDS_REVIEW"):
            assert row["NOTE"] is None
            assert row["DECISION_DATE_KEY"] is None
        else:
            assert row["NOTE"]
            assert row["DECISION_DATE_KEY"] > 0


def test_only_approved_lines_have_a_confirmed_quantity(histories):
    for row in decisions.restock_request_rows(histories):
        if row["_request_status"] in ("APPROVED", "FULFILLING", "COMPLETED"):
            assert row["CONFIRMED_QTY"] > 0
        else:
            assert row["CONFIRMED_QTY"] == 0


def test_generation_is_deterministic(histories):
    assert decisions.restock_request_rows(histories) == decisions.restock_request_rows(histories)
    assert production.execution_rows() == production.execution_rows()
    assert procurement.open_po_rows(histories) == procurement.open_po_rows(histories)


@pytest.mark.parametrize("horizon", [7, 14, 30])
def test_requirements_scale_with_the_horizon(horizon):
    base = production.part_requirements(7)
    scaled = production.part_requirements(horizon)
    part_id = next(iter(base))
    assert scaled[part_id] == pytest.approx(base[part_id] * horizon / 7)
