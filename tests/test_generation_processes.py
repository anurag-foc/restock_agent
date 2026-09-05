"""Tests for the scenario catalog, contracts, and demand processes.

Several of these exist because they caught real bugs during the build, and are kept so the bug
cannot come back:

- `test_every_declared_finding_is_planted_on_a_pair` — F11's two warehouses were absent from the
  stocked universe, so the ranking-inversion finding could never fire, with no error anywhere.
- `test_regime_shares_match_the_spec` — the regime cycle was ordered in blocks, which skewed
  smooth to 36% and erratic to 8% (target 12%) at a working set that is not a multiple of 100.
- `test_realised_cv_tracks_the_target` — a clipped normal at CV 0.9 truncates its left tail and
  realised only 0.59.
- `test_burn_scales_with_production_not_inversely_with_cost` — burn was originally inverse to
  unit cost, which is backwards for vehicle manufacturing and drove expensive parts under
  1 unit/day, where integer rounding makes a "smooth" label meaningless.
"""

from collections import Counter

import numpy as np
import pytest

from agentic_restock.generation import contracts, demand, entities, scenarios

# --- scenario catalog -------------------------------------------------------


def test_catalog_is_structurally_consistent():
    assert scenarios.validate_catalog() == []


def test_every_declared_finding_is_planted_on_a_pair():
    planted = {f for r in scenarios.pair_universe() for f in r["finding_ids"]}
    declared = set(scenarios.FINDINGS) - {"F10"}  # F10 is a property of the whole set
    assert declared <= planted, declared - planted


def test_required_pairs_are_all_stocked():
    universe = {(r["part_id"], r["warehouse_id"]) for r in scenarios.pair_universe()}
    for part_id, warehouse_id, role in scenarios.required_pairs():
        assert (part_id, warehouse_id) in universe, role


def test_intermittent_spares_do_not_expect_a_finding():
    """Silence is the correct behaviour for F9 — counting it as expected scores it as a miss."""
    for part_id in scenarios.FINDINGS["F9"]["parts"]:
        assert not scenarios.expect_finding(part_id, scenarios.FINDINGS["F9"]["warehouse"])


def test_quiet_majority_holds():
    universe = scenarios.pair_universe()
    expecting = [r for r in universe if scenarios.expect_finding(r["part_id"], r["warehouse_id"])]
    quiet_fraction = 1 - len(expecting) / len(universe)
    assert quiet_fraction >= scenarios.FINDINGS["F10"]["min_quiet_fraction"]


def test_f1_donor_ranking_is_non_trivial():
    """The decoy donor must be a different warehouse from the best donor, or there is no test."""
    f1 = scenarios.FINDINGS["F1"]
    assert f1["decoy_donor"] != f1["best_donor"] != f1["receiver"]


def test_f2_binding_children_descend_from_their_parent():
    from agentic_restock.generation import bom

    f2 = scenarios.FINDINGS["F2"]
    descendants = set(bom.descendants_of(f2["parent_assembly"]))
    assert len(f2["binding_children"]) >= 2
    for child in f2["binding_children"]:
        assert child in descendants


# --- contracts --------------------------------------------------------------


def test_every_bought_part_has_a_contract():
    components = {p["PART_ID"] for p in entities.parts() if p["BOM_LEVEL"] == 2}
    contracted = {r["part_id"] for r in contracts.contracts()}
    assert components == contracted


def test_each_part_has_exactly_one_preferred_supplier():
    preferred = Counter(r["part_id"] for r in contracts.contracts() if r["is_preferred"])
    assert set(preferred.values()) == {1}


def test_all_four_moq_regimes_are_present():
    regimes = {(r["moq"], r["pack_size"]) for r in contracts.contracts()}
    assert (10, 1) in regimes  # no constraint
    assert (200, 1) in regimes  # mild overbuy
    assert any(pack > 1 for _, pack in regimes)  # awkward pack
    assert any(moq >= 500 for moq, _ in regimes)  # F6's uneconomic MOQ


def test_the_cheapest_quote_is_the_least_reliable_supplier():
    """F5: effective_unit_cost must be able to reverse the quoted-price ranking."""
    f5 = scenarios.FINDINGS["F5"]
    quotes = [r for r in contracts.contracts() if r["part_id"] == f5["part"]]
    assert len(quotes) == 3
    cheapest = min(quotes, key=lambda r: r["unit_cost"])
    archetypes = {s["SUPPLIER_ID"]: s["_archetype"] for s in entities.suppliers()}
    assert archetypes[cheapest["supplier_id"]] == "cheap_and_bad"
    # And it is what today's process would pick, so the finding has something to overturn.
    assert cheapest["is_preferred"]


def test_reliable_suppliers_quote_higher_than_unreliable_ones():
    factors = contracts.PRICE_FACTOR_BY_ARCHETYPE
    assert factors["cheap_and_bad"] < factors["loose"] < factors["tight"]


def test_erratic_supplier_has_no_drift_but_a_wide_spread():
    f4 = scenarios.FINDINGS["F4"]
    mu_erratic, sigma_erratic = contracts.true_lead_parameters(f4["erratic_supplier"], 40)
    mu_drift, sigma_drift = contracts.true_lead_parameters(f4["drifting_supplier"], 40)
    assert mu_erratic == 40.0  # exactly on contract
    assert sigma_erratic > 10
    assert mu_drift > 40.0  # genuinely late
    assert sigma_drift < 5  # but predictably so


# --- demand processes -------------------------------------------------------


def test_generation_is_deterministic():
    first = demand.pair_params()
    assert first == demand.pair_params()
    key = next(iter(first))
    assert np.array_equal(demand.issue_series(first[key]), demand.issue_series(first[key]))


def test_regime_shares_match_the_spec():
    params = demand.pair_params()
    shares = Counter(v.regime for v in params.values())
    total = len(params)
    # Wider tolerance on intermittent: slow movers are reclassified into it on purpose.
    assert 0.25 <= shares["smooth"] / total <= 0.35
    assert 0.18 <= shares["seasonal"] / total <= 0.28
    assert 0.07 <= shares["erratic"] / total <= 0.16
    assert 0.12 <= shares["intermittent"] / total <= 0.24


def test_every_regime_has_enough_members_to_not_be_dead_code():
    shares = Counter(v.regime for v in demand.pair_params().values())
    assert set(shares) == set(demand._NOISE_CV)
    assert all(n >= 3 for n in shares.values()), shares


def test_all_three_history_cohorts_are_populated():
    """The shrinkage guard is inactive at 36 months, so short-history pairs are mandatory."""
    cohorts = Counter(v.history_cohort for v in demand.pair_params().values())
    assert cohorts["shrunk"] == scenarios.SHRUNK_HISTORY_PAIRS
    assert cohorts["sparse"] == scenarios.SPARSE_HISTORY_PAIRS
    assert cohorts["full"] > 200


def test_short_history_never_lands_on_a_finding_bearing_pair():
    """A truncated history would be indistinguishable from a detector failure."""
    for (part_id, warehouse_id), params in demand.pair_params().items():
        if params.history_cohort != "full":
            assert not scenarios.expect_finding(part_id, warehouse_id)


@pytest.mark.parametrize("regime", ["smooth", "erratic"])
def test_realised_cv_tracks_the_target(regime):
    params = [
        v
        for v in demand.pair_params().values()
        if v.regime == regime and v.quiet_since_day is None
    ][:25]
    realised = []
    for p in params:
        series = demand.issue_series(p).astype(float)
        series = series[series > 0]
        if len(series) > 10:
            realised.append(series.std() / series.mean())
    assert np.mean(realised) == pytest.approx(demand._NOISE_CV[regime], rel=0.25)


def test_intermittent_series_are_mostly_zero():
    """Above 0.7 is what routes the burn estimator to Croston instead of a daily mean."""
    params = [v for v in demand.pair_params().values() if v.regime == "intermittent"][:30]
    fractions = [(demand.issue_series(p) == 0).mean() for p in params]
    assert min(fractions) > 0.7


def test_burn_scales_with_production_not_inversely_with_cost():
    params = demand.pair_params()
    plant = [v.level for v in params.values() if v.warehouse_id in ("WH001", "WH002")]
    rdc = [v.level for v in params.values() if v.warehouse_id not in ("WH001", "WH002")]
    # Plant stores feed the line; RDCs carry aftermarket demand at a few percent of that.
    assert np.median(plant) > np.median(rdc) * 5


def test_non_intermittent_levels_clear_the_rounding_floor():
    for params in demand.pair_params().values():
        if params.regime != "intermittent":
            assert params.level >= demand.MIN_SMOOTH_LEVEL


def test_step_change_shifts_demand_by_the_declared_factor():
    f7 = scenarios.FINDINGS["F7"]
    params = demand.pair_params()[(f7["part"], f7["warehouse"])]
    series = demand.issue_series(params)
    pre = series[: params.step_day].mean()
    post = series[params.step_day :].mean()
    assert post / pre == pytest.approx(f7["step_factor"], rel=0.1)


def test_safety_stock_is_calibrated_to_the_pre_step_rate():
    """F7's whole point: the buffer was right before the shift and is wrong now.

    Asserts the relationship rather than a fixed number of days — safety stock is lead-time
    aware (`z x sigma_lead`, floored), so there is no single correct day count to compare to.
    """
    f7 = scenarios.FINDINGS["F7"]
    params = demand.pair_params()[(f7["part"], f7["warehouse"])]
    series = demand.issue_series(params)

    pre_cover = params.safety_stock / series[: params.step_day].mean()
    post_cover = params.safety_stock / series[params.step_day :].mean()

    # Sized off the pre-step rate, so it still looks adequate against the old demand...
    assert pre_cover >= demand.MIN_SAFETY_DAYS * 0.9
    # ...and is materially thin against the new one. That gap is the finding.
    assert post_cover == pytest.approx(pre_cover / f7["step_factor"], rel=0.15)


def test_safety_stock_is_lead_time_aware():
    """A flat day count leaves long-lead parts structurally short — 57% of pairs, when tried.

    Measured against `level`, the rate the buffer was sized for — not `effective_level`. A
    step-change pair's buffer *is* thin against its current demand, and that gap is F7's finding
    rather than a policy error.
    """
    params = demand.pair_params()
    days = {p.safety_stock / p.level for p in params.values() if p.level > 0}
    assert min(days) >= demand.MIN_SAFETY_DAYS * 0.9
    # Erratic suppliers must force a bigger buffer than consistent ones.
    assert max(days) > demand.MIN_SAFETY_DAYS * 1.5


def test_step_pairs_are_the_only_ones_under_buffered_against_current_demand():
    params = demand.pair_params()
    thin = {
        key
        for key, p in params.items()
        if p.effective_level > 0 and p.safety_stock / p.effective_level < demand.MIN_SAFETY_DAYS * 0.9
    }
    assert thin, "F7 needs at least one under-buffered pair"
    for key in thin:
        assert params[key].step_day is not None, key
        assert params[key].step_factor > 1.0, key


def test_dead_capital_pairs_stop_issuing():
    for part_id, warehouse_id in scenarios.FINDINGS["F8"]["pairs"]:
        params = demand.pair_params()[(part_id, warehouse_id)]
        series = demand.issue_series(params)
        assert params.quiet_since_day is not None
        assert series[params.quiet_since_day :].sum() == 0
        assert series[: params.quiet_since_day].sum() > 0
