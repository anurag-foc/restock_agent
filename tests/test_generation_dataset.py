"""Tests for the assembled dataset, snapshot sampling, ground truth, and the self-assertions.

The §5 assertions are the real gate, so most of this file runs them. The rest guards properties
that would otherwise only surface at load time:

- `test_snapshot_sampling_preserves_the_audit_property` — snapshots are sampled, not daily; if
  that ever broke reconciliation, the dataset would silently stop being auditable.
- `test_ground_truth_records_both_true_and_observable_values` — a gate that graded an estimator
  against a `true_` column would penalise it for data deliberately withheld.
- `test_no_load_helper_column_is_treated_as_a_table_column` — `_request_status` and friends are
  join inputs; emitting them as columns would fail the INSERT.
"""

import pytest

from agentic_restock.generation import (
    assertions,
    dataset,
    entities,
    ground_truth,
    scenarios,
    snapshots,
)


@pytest.fixture(scope="session")
def data():
    return dataset.build()


# --- the §5 gate ------------------------------------------------------------


def test_all_self_assertions_pass():
    """docs/dataset_generator_spec.md §5. Determinism is covered separately (it rebuilds twice)."""
    results = assertions.run_all(include_determinism=False)
    assert assertions.failures(results) == []


def test_generation_is_deterministic():
    assert assertions.check_determinism() == []


# --- assembly ---------------------------------------------------------------


def test_every_table_has_a_load_target(data):
    assert set(data) == set(dataset.TARGETS)


def test_no_table_is_empty(data):
    empty = [name for name, rows in data.items() if not rows]
    assert empty == []


def test_no_load_helper_column_is_treated_as_a_table_column(data):
    """Columns prefixed '_' are join inputs, not columns; emitting them fails the INSERT."""
    for name, rows in data.items():
        columns = dataset.table_columns(rows)
        assert not [c for c in columns if c.startswith("_")], name


def test_restock_requests_carry_status_by_name_for_the_loader(data):
    """dim_request_status is a real 17-row dimension with hash keys and is not regenerated."""
    rows = data["fact_restock_request"]
    assert all(row["_request_status"] for row in rows)
    assert all(row["_urgency_level"] for row in rows)
    assert "REQUEST_STATUS_KEY" not in dataset.table_columns(rows)


def test_surrogate_keys_are_unique_within_each_table(data):
    for name, rows in data.items():
        key_column = next(
            (c for c in dataset.table_columns(rows) if c.endswith("_KEY")
             and c.replace("_KEY", "").lower() in name.replace("fact_", "").replace("dim_", "")),
            None,
        )
        if key_column is None:
            continue
        values = [row[key_column] for row in rows]
        assert len(set(values)) == len(values), f"{name}.{key_column} has duplicates"


# --- snapshot sampling ------------------------------------------------------


def test_snapshots_are_sampled_not_daily(data):
    """278 pairs x 1100 days is 293,600 rows and nothing needs them all."""
    rows = data["fact_inventory_snapshot"]
    assert len(rows) < 60_000
    assert len(rows) > 30_000


def test_every_pair_has_a_snapshot_dated_today(data):
    """The detectors read the latest snapshot per pair; a pair without one is invisible."""
    from agentic_restock.generation import dates

    today = dates.date_key(dates.TODAY)
    pairs_today = {
        (row["PART_KEY"], row["WAREHOUSE_KEY"])
        for row in data["fact_inventory_snapshot"]
        if row["SNAPSHOT_DATE_KEY"] == today
    }
    all_pairs = {
        (row["PART_KEY"], row["WAREHOUSE_KEY"]) for row in data["fact_inventory_snapshot"]
    }
    assert pairs_today == all_pairs


def test_snapshot_sampling_preserves_the_audit_property(data):
    assert assertions.check_snapshot_ties_to_transactions(data) == []


def test_transactions_keep_full_daily_fidelity(data):
    """They are the burn estimator's only input; thinning them degrades what Phase 2 measures."""
    rows = data["fact_inventory_transaction"]
    issues = [r for r in rows if r["TRANSACTION_TYPE"] == snapshots.TXN_ISSUE]
    receipts = [r for r in rows if r["TRANSACTION_TYPE"] == snapshots.TXN_RECEIPT]
    assert len(issues) > 150_000
    assert len(receipts) > 5_000


def test_avg_daily_consumption_is_the_naive_flat_mean(data):
    """Written wrong on purpose, so a detector using it instead of corrected burn is worse."""
    from agentic_restock.generation import replenishment

    histories = replenishment.simulate_all()
    part_ids = {p["PART_KEY"]: p["PART_ID"] for p in entities.parts()}
    warehouse_ids = {w["WAREHOUSE_KEY"]: w["WAREHOUSE_ID"] for w in entities.warehouses()}

    checked = 0
    for row in data["fact_inventory_snapshot"][:400]:
        pair = (part_ids[row["PART_KEY"]], warehouse_ids[row["WAREHOUSE_KEY"]])
        history = histories[pair]
        trailing = history.issues[-365:] if len(history.issues) >= 365 else history.issues
        assert row["AVG_DAILY_CONSUMPTION"] == pytest.approx(trailing.mean(), abs=0.01)
        checked += 1
    assert checked > 0


# --- ground truth -----------------------------------------------------------


def test_ground_truth_covers_every_pair_and_contracted_supplier(data):
    from agentic_restock.generation import contracts

    rows = data["sim_ground_truth"]
    pairs = [r for r in rows if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_PAIR]
    supplier_parts = [r for r in rows if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_SUPPLIER_PART]
    assert len(pairs) == len(scenarios.pair_universe())
    assert len(supplier_parts) == len(contracts.contracted_pairs())


def test_ground_truth_records_both_true_and_observable_values(data):
    """They differ on purpose; grading against `true_` penalises withheld data."""
    supplier_parts = [
        r for r in data["sim_ground_truth"]
        if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_SUPPLIER_PART
    ]
    with_capture = [r for r in supplier_parts if r["OBSERVED_DELIVERY_N"] > 0]
    assert with_capture
    assert all(r["TRUE_SIGMA_LEAD_DAYS"] is not None for r in with_capture)
    assert all(r["OBSERVED_SIGMA_DELAY_DAYS"] is not None for r in with_capture)
    # And the contracted-only tier has no observation at all, which is the point of that tier.
    without = [r for r in supplier_parts if r["OBSERVED_DELIVERY_N"] == 0]
    assert without
    assert all(r["OBSERVED_MEAN_DELAY_DAYS"] is None for r in without)


def test_step_pairs_record_the_post_step_rate_as_the_recoverable_one(data):
    pairs = [
        r for r in data["sim_ground_truth"]
        if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_PAIR and r["REGIME"] == "step"
    ]
    assert pairs
    for row in pairs:
        assert row["TRUE_EFFECTIVE_LEVEL"] == pytest.approx(
            row["TRUE_LEVEL"] * row["TRUE_STEP_FACTOR"], rel=1e-3
        )
        assert row["STEP_DATE_KEY"] > 0


def test_expect_finding_excludes_the_intermittent_negative_cases(data):
    """F9's correct behaviour is silence; counting it as expected scores it as a miss."""
    pairs = {
        r["SUBJECT_ID"]: r
        for r in data["sim_ground_truth"]
        if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_PAIR
    }
    f9 = scenarios.FINDINGS["F9"]
    for part_id in f9["parts"]:
        row = pairs[f"{part_id}@{f9['warehouse']}"]
        assert row["PLANTED_FINDING_IDS"] == "F9"
        assert row["EXPECT_FINDING"] is False


def test_every_data_backed_fallback_tier_appears_in_ground_truth(data):
    """`contracted` is excluded on purpose — see `assertions.DATA_BACKED_FALLBACK_TIERS`."""
    from collections import Counter

    tiers = Counter(
        r["EXPECTED_FALLBACK_TIER"]
        for r in data["sim_ground_truth"]
        if r["SUBJECT_TYPE"] == ground_truth.SUBJECT_SUPPLIER_PART
    )
    for tier in assertions.DATA_BACKED_FALLBACK_TIERS:
        assert tiers[tier] >= assertions.MIN_PAIRS_PER_FALLBACK_TIER, tiers
