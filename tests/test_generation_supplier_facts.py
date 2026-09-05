"""Tests for the supplier delivery and quality facts.

Guards two bugs found during the build:

- `test_no_pair_exceeds_its_capture_tier_cap` — the cap was applied per (part, warehouse) while
  E2 fits at (part, supplier), so four warehouses stocking a part multiplied 40 into 160 and
  every capture tier collapsed into "rich".
- `test_reliable_suppliers_are_mostly_on_time` — with `tight` suppliers aiming at exactly the
  contracted date, a symmetric distribution made them late half the time and overall OTD came
  out at 33%.
"""

import numpy as np

from agentic_restock.generation import (
    contracts,
    entities,
    replenishment,
    scenarios,
    supplier_facts,
)

# --- capture tiers ----------------------------------------------------------


def test_all_four_capture_tiers_are_used():
    tiers = set(supplier_facts.capture_tiers().values())
    assert tiers == set(supplier_facts.CAPTURE_TIERS)


def test_no_pair_exceeds_its_capture_tier_cap(lead_summary):
    assert max(v["n"] for v in lead_summary.values()) <= supplier_facts.CAPTURE_TIERS["rich"]


def test_every_lead_time_fallback_tier_is_reachable(lead_summary):
    """(supplier,part) -> (supplier) -> (category) -> contracted. All four, or the ladder is untested."""
    counts = [v["n"] for v in lead_summary.values()]
    assert sum(1 for n in counts if n >= 8) >= 5  # pair tier
    assert sum(1 for n in counts if 3 <= n < 8) >= 3  # supplier tier
    assert sum(1 for n in counts if 1 <= n < 3) >= 3  # category tier
    assert len(contracts.contracted_pairs()) - len(lead_summary) >= 3  # contracted only


def test_finding_bearing_suppliers_always_get_rich_capture():
    """Withholding a finding's evidence is indistinguishable from a detector failure."""
    tiers = supplier_facts.capture_tiers()
    f4 = scenarios.FINDINGS["F4"]
    for part_id in f4["parts"]:
        supplier = contracts.preferred_supplier(part_id)
        assert tiers[(part_id, supplier)] == "rich"
    for supplier_id, _price in scenarios.FINDINGS["F5"]["suppliers"]:
        assert tiers[(scenarios.FINDINGS["F5"]["part"], supplier_id)] == "rich"


# --- the columns that were entirely NULL in the replica ---------------------


def test_no_sentinel_dates_remain(delivery_records):
    rows = supplier_facts.delivery_rows(delivery_records)
    assert rows
    for row in rows:
        assert row["DELIVERY_DATE_KEY"] > 0
        assert row["PLANNED_DATE_KEY"] > 0


def test_freight_cost_is_populated(delivery_records):
    """The column exists but has never carried data; FX1 needs it to quote a transfer's cost."""
    rows = supplier_facts.delivery_rows(delivery_records)
    assert all(row["FREIGHT_COST"] > 0 for row in rows)


def test_part_key_is_populated(delivery_records):
    """The column this project added, so lead time can be measured per part."""
    rows = supplier_facts.delivery_rows(delivery_records)
    assert all(row["PART_KEY"] > 0 for row in rows)


def test_quality_measures_are_populated(delivery_records):
    """INSPECTED_QTY / DEFECT_QTY / QUALITY_SCORE are NULL on all 1100 replica rows today."""
    rows = supplier_facts.quality_rows(delivery_records)
    assert rows
    for row in rows:
        assert row["INSPECTED_QTY"] > 0
        assert row["DEFECT_QTY"] >= 0
        assert 0 <= row["QUALITY_SCORE"] <= 100
    assert any(row["DEFECT_QTY"] > 0 for row in rows)


# --- behavioural properties -------------------------------------------------


def test_reliable_suppliers_are_mostly_on_time(lead_summary):
    by_archetype: dict[str, list[float]] = {}
    for (_part, supplier_id), stats in lead_summary.items():
        by_archetype.setdefault(contracts.archetype_of(supplier_id), []).append(stats["otd_rate"])
    assert np.mean(by_archetype["tight"]) > 0.8
    assert np.mean(by_archetype["drifting"]) < 0.3


def test_erratic_supplier_has_wide_spread_without_mean_drift(lead_summary):
    """F4, measured on the generated data rather than on the archetype declaration."""
    f4 = scenarios.FINDINGS["F4"]
    erratic_part = f4["parts"][0]
    stats = lead_summary[(erratic_part, contracts.preferred_supplier(erratic_part))]
    assert abs(stats["observed_mean_delay"]) < 4  # on contract, on average
    assert stats["observed_sigma_delay"] > 8  # but unpredictable


def test_drifting_supplier_is_consistently_late(lead_summary):
    f4 = scenarios.FINDINGS["F4"]
    drifting_part = f4["parts"][1]
    stats = lead_summary[(drifting_part, contracts.preferred_supplier(drifting_part))]
    assert stats["observed_mean_delay"] > 4  # genuinely late
    assert stats["observed_sigma_delay"] < 6  # but predictably so


def test_improving_supplier_is_better_recently_than_historically():
    """Without this the archetype is indistinguishable from `drifting`, and E2's recency
    weighting — which stops a supplier being condemned for last year — has nothing to detect."""
    supplier = entities.supplier_with_archetype("improving")
    old_mu, _ = contracts.true_lead_parameters_at(supplier, 40, days_ago=900)
    recent_mu, _ = contracts.true_lead_parameters_at(supplier, 40, days_ago=30)
    assert recent_mu < old_mu - 5


def test_defect_rates_track_the_archetype(delivery_records):
    by_archetype: dict[str, list[float]] = {}
    for record in delivery_records:
        rate = (record.damaged_qty + record.short_qty) / max(record.quantity, 1)
        by_archetype.setdefault(
            contracts.archetype_of(record.event.supplier_id), []
        ).append(rate)
    assert np.mean(by_archetype["cheap_and_bad"]) > np.mean(by_archetype["tight"]) * 3


def test_every_delivery_corresponds_to_a_stock_movement(delivery_records):
    """Deliveries are drawn from receipt events, so the two facts describe one world."""
    assert all(record.quantity > 0 for record in delivery_records)
    assert all(record.event.arrival_day > record.event.order_day for record in delivery_records)


def test_generation_is_deterministic():
    histories = replenishment.simulate_all()
    first = supplier_facts.delivery_rows(supplier_facts.delivery_records(histories))
    second = supplier_facts.delivery_rows(supplier_facts.delivery_records(histories))
    assert first == second
