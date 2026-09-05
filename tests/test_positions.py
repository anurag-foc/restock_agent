"""Tests for the corrected-measures layer.

Driven by the generator's own rows rather than by fixtures, so the frames under test have the
same shape the SQL returns from the live tables. The queries themselves are validated
separately against Databricks; what is tested here is everything that happens after the read.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from agentic_restock.estimators import burn as e1
from agentic_restock.estimators import leadtime as e2
from agentic_restock.generation import dataset, dates, entities
from agentic_restock.jobs import positions


@pytest.fixture(scope="module")
def frames():
    """Query-shaped DataFrames, rebuilt from the generated rows."""
    data = dataset.build()

    part_ids = {p["PART_KEY"]: p["PART_ID"] for p in entities.parts()}
    warehouse_ids = {w["WAREHOUSE_KEY"]: w["WAREHOUSE_ID"] for w in entities.warehouses()}
    supplier_ids = {s["SUPPLIER_KEY"]: s["SUPPLIER_ID"] for s in entities.suppliers()}
    categories = {s["SUPPLIER_ID"]: s["SUPPLIER_TYPE"] for s in entities.suppliers()}
    parts_by_id = {p["PART_ID"]: p for p in entities.parts()}

    issues = pd.DataFrame(
        [
            {
                "PART_ID": part_ids[r["PART_KEY"]],
                "WAREHOUSE_ID": warehouse_ids[r["WAREHOUSE_KEY"]],
                "TRANSACTION_DATE_KEY": r["TRANSACTION_DATE_KEY"],
                "ISSUE_QTY": r["QUANTITY"],
            }
            for r in data["fact_inventory_transaction"]
            if r["TRANSACTION_TYPE"] == "ISSUE"
        ]
    )

    # Latest snapshot per pair, as the query's ROW_NUMBER would pick.
    snapshot = pd.DataFrame(data["fact_inventory_snapshot"])
    latest = (
        snapshot.sort_values(["SNAPSHOT_DATE_KEY", "INVENTORY_SNAPSHOT_KEY"])
        .groupby(["PART_KEY", "WAREHOUSE_KEY"], as_index=False)
        .last()
    )
    inbound = (
        pd.DataFrame(data["fact_procurement"])
        .groupby("PART_KEY")["PENDING_QTY"]
        .sum()
        .to_dict()
    )
    position = pd.DataFrame(
        [
            {
                "PART_ID": part_ids[r.PART_KEY],
                "WAREHOUSE_ID": warehouse_ids[r.WAREHOUSE_KEY],
                "PART_KEY": r.PART_KEY,
                "WAREHOUSE_KEY": r.WAREHOUSE_KEY,
                "QUANTITY_ON_HAND": r.QUANTITY_ON_HAND,
                "SAFETY_STOCK_QTY": r.SAFETY_STOCK_QTY,
                "MAX_STOCK_LEVEL": r.MAX_STOCK_LEVEL,
                "NAIVE_DAILY_CONSUMPTION": r.AVG_DAILY_CONSUMPTION,
                "IN_TRANSIT_QTY": inbound.get(r.PART_KEY, 0),
                "UNIT_COST": parts_by_id[part_ids[r.PART_KEY]]["UNIT_COST"],
                "CRITICALITY_CLASS": parts_by_id[part_ids[r.PART_KEY]]["CRITICALITY_CLASS"],
                "BOM_LEVEL": parts_by_id[part_ids[r.PART_KEY]]["BOM_LEVEL"],
                "ABC_CLASS": parts_by_id[part_ids[r.PART_KEY]]["ABC_CLASS"],
                "WAREHOUSE_TYPE": "REGIONAL_DC",
                "OPERATIONAL_STATUS": "ACTIVE",
            }
            for r in latest.itertuples(index=False)
        ]
    )

    delivery = pd.DataFrame(
        [
            {
                "PART_ID": part_ids[r["PART_KEY"]],
                "SUPPLIER_ID": supplier_ids[r["SUPPLIER_KEY"]],
                "SUPPLIER_CATEGORY": categories[supplier_ids[r["SUPPLIER_KEY"]]],
                "DELIVERY_DATE_KEY": r["DELIVERY_DATE_KEY"],
                "PLANNED_DATE_KEY": r["PLANNED_DATE_KEY"],
                "DELAY_DAYS": r["DELAY_DAYS"],
                "QUANTITY": r["QUANTITY"],
                "DAMAGED_QTY": r["DAMAGED_QTY"],
                "SHORT_QTY": r["SHORT_QTY"],
                "FREIGHT_COST": r["FREIGHT_COST"],
                "OTD_FLAG": r["OTD_FLAG"],
            }
            for r in data["fact_supplier_delivery"]
        ]
    )

    contracts = pd.DataFrame(
        [
            {
                "PART_ID": r["part_id"],
                "SUPPLIER_ID": r["supplier_id"],
                "CONTRACTED_LEAD_DAYS": r["lead_time_days"],
                "MOQ": r["moq"],
                "PACK_SIZE": r["pack_size"],
                "CONTRACT_UNIT_COST": r["unit_cost"],
                "IS_PREFERRED": r["is_preferred"],
            }
            for r in data["dim_supplier_contract"]
        ]
    )

    return {
        "issues": issues,
        "position": position,
        "delivery": delivery,
        "contracts": contracts,
    }


@pytest.fixture(scope="module")
def supplier_performance(frames):
    return positions.build_supplier_performance(
        frames["delivery"], frames["contracts"], as_of=dates.TODAY
    )


@pytest.fixture(scope="module")
def part_position(frames, supplier_performance):
    return positions.build_part_position(
        frames["position"], frames["issues"], supplier_performance, as_of=dates.TODAY
    )


# --- queries are strings; check they name the right things ------------------


def test_queries_reference_the_configured_catalog():
    for query in (
        positions.build_issue_history_query(),
        positions.build_current_position_query(),
        positions.build_delivery_history_query(),
        positions.build_contract_query(),
    ):
        assert query.strip()
        assert "gold_dev" in query


def test_issue_query_filters_to_issue_transactions():
    """RECEIPT rows are stock arriving, not consumption — including them inflates every burn."""
    query = positions.build_issue_history_query()
    assert "TRANSACTION_TYPE = 'ISSUE'" in query


def test_position_query_takes_the_latest_snapshot_per_pair():
    """The snapshot is a daily fact; without this every pair appears ~1100 times."""
    query = positions.build_current_position_query()
    assert "ROW_NUMBER()" in query
    assert "rn = 1" in query
    # Tiebreak on the surrogate key too — two rows can share a date.
    assert "INVENTORY_SNAPSHOT_KEY DESC" in query


def test_delivery_query_uses_the_part_key_this_project_added():
    assert "d.PART_KEY" in positions.build_delivery_history_query()


# --- densification ----------------------------------------------------------


def test_densify_restores_the_zero_days(frames):
    """The zero days ARE the intermittency signal. Reading only the days a part moved would make
    every part look smooth and route nothing to Croston."""
    series = positions.densify_issues(frames["issues"], as_of=dates.TODAY, history_days=1100)
    assert series
    lengths = {len(v) for v in series.values()}
    assert lengths == {1100}
    zero_fractions = [float((v == 0).mean()) for v in series.values()]
    assert max(zero_fractions) > e1.INTERMITTENCY_THRESHOLD


def test_densify_preserves_total_quantity(frames):
    series = positions.densify_issues(frames["issues"], as_of=dates.TODAY, history_days=1100)
    from_series = sum(float(v.sum()) for v in series.values())
    from_rows = float(frames["issues"]["ISSUE_QTY"].sum())
    assert from_series == pytest.approx(from_rows, rel=1e-9)


def test_densify_handles_an_empty_frame():
    assert positions.densify_issues(pd.DataFrame(), as_of=dates.TODAY) == {}


# --- supplier_performance ---------------------------------------------------


def test_supplier_performance_covers_every_contract(frames, supplier_performance):
    assert len(supplier_performance) == len(frames["contracts"])
    assert supplier_performance["LEAD_TIER"].notna().all()


def test_supplier_performance_carries_a_measured_spread(supplier_performance):
    measured = supplier_performance[supplier_performance["IS_MEASURED"]]
    assert len(measured) > 20
    assert (measured["SIGMA_LEAD_DAYS"] > 0).all()


def test_freight_cost_is_available_for_the_transfer_fix(supplier_performance):
    """FX1 needs a real freight figure rather than describing the downside in words."""
    assert (supplier_performance["MEAN_FREIGHT_COST"] > 0).any()


def test_reject_rate_differentiates_suppliers(supplier_performance):
    rates = supplier_performance["REJECT_RATE"]
    assert rates.max() > rates.median() * 2


# --- part_position ----------------------------------------------------------


def test_one_row_per_stocked_pair(frames, part_position):
    assert len(part_position) == len(frames["position"])
    assert not part_position.duplicated(["PART_ID", "WAREHOUSE_ID"]).any()


def test_availability_is_on_hand_plus_inbound(part_position):
    computed = part_position["ON_HAND_QTY"] + part_position["IN_TRANSIT_QTY"]
    assert (part_position["AVAILABLE_QTY"] == computed).all()


def test_days_of_cover_is_null_when_nothing_is_moving(part_position):
    """A sentinel large number would be silently ranked and compared; NULL forces the
    dead-capital case to be handled as the different question it is."""
    stopped = part_position[part_position["FORWARD_BURN"] <= 0]
    assert len(stopped) > 0
    assert stopped["DAYS_OF_COVER"].isna().all()


def test_days_of_cover_matches_availability_over_burn(part_position):
    moving = part_position[part_position["FORWARD_BURN"] > 0]
    expected = moving["AVAILABLE_QTY"] / moving["FORWARD_BURN"]
    assert np.allclose(moving["DAYS_OF_COVER"], expected)


def test_the_corrected_burn_differs_from_the_snapshot_average(part_position):
    """Carrying both makes the correction auditable. If they agreed everywhere, the estimator
    would be adding nothing over the number the old board already used."""
    both = part_position[
        (part_position["FORWARD_BURN"] > 0) & (part_position["NAIVE_DAILY_CONSUMPTION"] > 0)
    ]
    ratio = both["FORWARD_BURN"] / both["NAIVE_DAILY_CONSUMPTION"]
    assert (ratio.sub(1).abs() > 0.1).mean() > 0.2


def test_every_burn_method_and_confidence_is_represented(part_position):
    methods = set(part_position["BURN_METHOD"])
    assert e1.METHOD_CROSTON in methods
    assert e1.METHOD_SEASONAL in methods
    confidences = set(part_position["BURN_CONFIDENCE"])
    assert {e1.CONFIDENCE_HIGH, e1.CONFIDENCE_MEDIUM, e1.CONFIDENCE_LOW} <= confidences


def test_lead_time_reaches_the_position_row(part_position):
    """A pair whose part has a preferred supplier must carry that supplier's lead estimate, or
    the risk model has no sigma_L and the whole spread argument disappears."""
    with_supplier = part_position[part_position["PREFERRED_SUPPLIER_ID"].notna()]
    assert len(with_supplier) > 100
    assert (with_supplier["MU_LEAD_DAYS"] > 0).all()
    assert with_supplier["LEAD_TIER"].isin(
        [e2.TIER_PAIR, e2.TIER_SUPPLIER, e2.TIER_CATEGORY, e2.TIER_CONTRACTED]
    ).all()


def test_target_cover_exceeds_lead_time(part_position):
    """Derived as mu_lead + review + z*sigma_lead, so it must always clear the lead time."""
    measured = part_position[part_position["MU_LEAD_DAYS"] > 0]
    assert (measured["TARGET_COVER_DAYS"] > measured["MU_LEAD_DAYS"]).all()


def test_replenishment_gap_is_never_negative(part_position):
    """FX3 uses this as the order quantity input; a negative gap would round up to MOQ and order
    stock for a part that already has too much."""
    assert (part_position["REPLENISHMENT_GAP_QTY"] >= 0).all()


def test_positions_are_deterministic(frames, supplier_performance):
    first = positions.build_part_position(
        frames["position"], frames["issues"], supplier_performance, as_of=dates.TODAY
    )
    second = positions.build_part_position(
        frames["position"], frames["issues"], supplier_performance, as_of=dates.TODAY
    )
    pd.testing.assert_frame_equal(first, second)


def test_as_of_date_shifts_the_read_window(frames, supplier_performance):
    """The window ends at `as_of`, so an earlier date must see different recent demand."""
    earlier = positions.build_part_position(
        frames["position"],
        frames["issues"],
        supplier_performance,
        as_of=date(2025, 9, 4),
    )
    current = positions.build_part_position(
        frames["position"], frames["issues"], supplier_performance, as_of=dates.TODAY
    )
    assert not np.allclose(
        earlier["FORWARD_BURN"].fillna(0), current["FORWARD_BURN"].fillna(0)
    )
