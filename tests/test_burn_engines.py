"""The two consumption engines must be interchangeable, not merely both present.

`consumption_model` selects between `automatic` (our ladder) and `statsforecast` (Nixtla's
estimators behind the same contract). The simulation feature
(docs/simulation_feature_design.md) compares them on one drawn world and attributes the
difference in net value to the estimator — which is only a valid inference if *nothing else*
differs. So the tests here are mostly parity tests: same routing, same struct, same business
rules. The one place they must differ is the number, and there is a test for that too.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from agentic_restock.estimators import burn as e1

statsforecast = pytest.importorskip(
    "statsforecast", reason="the second engine's package is an optional extra"
)

END = date(2026, 9, 4)
HORIZON = 45
ENGINES = (e1.MODEL_AUTOMATIC, e1.MODEL_STATSFORECAST)


def _rng(seed: int = 7) -> np.random.Generator:
    return np.random.default_rng(seed)


@pytest.fixture
def lumpy() -> np.ndarray:
    """~88% zero days: the intermittent branch."""
    rng = _rng()
    return np.where(rng.random(1095) < 0.12, rng.integers(1, 9, 1095), 0).astype(float)


@pytest.fixture
def seasonal() -> np.ndarray:
    """Dense, with an annual cycle: the seasonal branch."""
    rng = _rng(11)
    t = np.arange(1095)
    return np.clip(20 + 8 * np.sin(2 * np.pi * t / 365) + rng.normal(0, 2, 1095), 0, None)


def _estimate(series: np.ndarray, model: str, horizon: int = HORIZON) -> e1.BurnEstimate:
    return e1.estimate_burn(series, end_date=END, horizon_days=horizon, model=model)


# --- routing parity ---------------------------------------------------------------------


@pytest.mark.parametrize("model", ENGINES)
def test_intermittent_series_route_to_an_intermittent_method(lumpy, model):
    """Both engines must agree a lumpy part is lumpy.

    If the engines disagreed on *routing*, a simulation comparing them would be measuring the
    ladder rather than the estimators, and a difference in net value could not be attributed.
    """
    assert _estimate(lumpy, model).is_intermittent


@pytest.mark.parametrize("model", ENGINES)
def test_dense_series_never_route_to_an_intermittent_method(seasonal, model):
    assert not _estimate(seasonal, model).is_intermittent


@pytest.mark.parametrize("model", ENGINES)
def test_short_history_reports_trailing_mean_and_low_confidence(seasonal, model):
    """Missing history is a property of the data. No engine gets to be confident about it."""
    estimate = _estimate(seasonal[-60:], model)
    assert estimate.method == e1.METHOD_TRAILING_MEAN
    assert estimate.confidence == e1.CONFIDENCE_LOW


@pytest.mark.parametrize("model", ENGINES)
def test_stopped_demand_reads_as_zero_in_both_engines(lumpy, model):
    """The STOPPED rule is a business rule, not a forecasting method.

    S4 DEAD_CAPITAL reads it. An engine that reported the rate a part *used to* run at would
    show healthy cover on stock that will never be consumed — the failure the staleness branch
    exists to prevent, and one no package would catch on our behalf.
    """
    stopped = lumpy.copy()
    stopped[-400:] = 0.0
    estimate = _estimate(stopped, model)
    assert estimate.method == e1.METHOD_STOPPED
    assert estimate.level == pytest.approx(0.0)
    assert estimate.forward_burn == pytest.approx(0.0)


# --- contract parity --------------------------------------------------------------------


@pytest.mark.parametrize("model", ENGINES)
@pytest.mark.parametrize("fixture", ["lumpy", "seasonal"])
def test_every_engine_reports_a_spread(model, fixture, request):
    """`risk.py` reads sigma_d, not the rate. An engine that returned only a point forecast
    would collapse sigma_DL and hand every part false certainty."""
    series = request.getfixturevalue(fixture)
    assert _estimate(series, model).sigma_d > 0


@pytest.mark.parametrize("model", ENGINES)
@pytest.mark.parametrize("fixture", ["lumpy", "seasonal"])
def test_burn_is_never_negative(model, fixture, request):
    """ETS is additive in levels and will project a declining series below zero. A negative
    forward burn reads downstream as infinite days of cover."""
    series = request.getfixturevalue(fixture)
    estimate = _estimate(series, model)
    assert estimate.forward_burn >= 0
    assert estimate.level >= 0


@pytest.mark.parametrize("model", ENGINES)
def test_a_declining_series_does_not_forecast_negative_demand(model):
    rng = _rng(3)
    t = np.arange(1095)
    declining = np.clip(40 - 0.035 * t + rng.normal(0, 1.5, 1095), 0, None)
    assert _estimate(declining, model).forward_burn >= 0


@pytest.mark.parametrize("model", ENGINES)
def test_seasonal_parts_get_a_forward_burn_that_differs_from_level(seasonal, model):
    """The level/forward_burn split is the whole reason E1 returns two numbers. An engine that
    collapsed them would make a seasonal part untestable — you could not tell a wrong level
    from a right level in a busy season."""
    estimate = _estimate(seasonal, model)
    assert estimate.forward_burn != pytest.approx(estimate.level, rel=0.02)
    assert estimate.seasonal_index_ahead != pytest.approx(1.0, abs=0.02)


# --- where they must NOT agree ----------------------------------------------------------


def test_sba_corrects_classic_crostons_upward_bias(lumpy):
    """The substantive difference on the intermittent branch.

    Classic Croston's rate is a ratio of two smoothed means, and the expectation of a ratio is
    not the ratio of expectations — it runs high. Syntetos-Boylan applies the correction, so
    SBA should sit *below* our unweighted-means estimator on the same series. If this ever
    flips, the two engines are no longer estimating what their names claim.
    """
    ours = _estimate(lumpy, e1.MODEL_AUTOMATIC)
    nixtla = _estimate(lumpy, e1.MODEL_STATSFORECAST)

    assert ours.method == e1.METHOD_CROSTON
    assert nixtla.method == e1.METHOD_SF_CROSTON_SBA
    assert 0 < nixtla.level < ours.level


def test_the_engines_agree_closely_on_a_well_behaved_seasonal_part(seasonal):
    """Not a parity requirement — a sanity check.

    On a clean annual cycle with three years of history there is a right answer, and two
    competent estimators should both find it. A large gap here would mean one of them is
    misreading the calendar, which is the failure mode monthly aggregation introduces.
    """
    ours = _estimate(seasonal, e1.MODEL_AUTOMATIC)
    nixtla = _estimate(seasonal, e1.MODEL_STATSFORECAST)

    assert nixtla.level == pytest.approx(ours.level, rel=0.10)
    assert nixtla.seasonal_index_ahead == pytest.approx(ours.seasonal_index_ahead, rel=0.10)


# --- selection behaviour ----------------------------------------------------------------


def test_an_unrecognised_model_still_falls_back_to_automatic(seasonal):
    """A settings row that predates a rename must not stop a scan."""
    assert _estimate(seasonal, "a_model_we_removed").method == e1.METHOD_SEASONAL


def test_a_missing_package_fails_loudly_and_names_itself(monkeypatch, seasonal):
    """The opposite of the case above, deliberately.

    An unknown setting is stale config; a known engine with no package is a broken deployment.
    Falling back silently would leave the admin panel showing `statsforecast` while the
    pipeline ran `automatic` — the "your system ignored me" failure
    tests/test_settings_spec_parity.py exists to prevent, except undiagnosable because the
    numbers would look fine.
    """
    def _no_package():
        raise ImportError(
            "consumption_model='statsforecast' requires the statsforecast package, which is "
            "not installed in this environment."
        )

    monkeypatch.setattr(e1, "_load_statsforecast", _no_package)
    with pytest.raises(ImportError, match="statsforecast"):
        _estimate(seasonal, e1.MODEL_STATSFORECAST)


def test_the_horizon_changes_the_forward_burn_of_a_seasonal_part(seasonal):
    """`forward_burn` answers "over the replenishment window", so a longer window covering
    more of the cycle must move it. If it did not, the engine would be reporting a flat rate
    with a seasonal label on it."""
    short_horizon = _estimate(seasonal, e1.MODEL_STATSFORECAST, horizon=15)
    long_horizon = _estimate(seasonal, e1.MODEL_STATSFORECAST, horizon=120)
    assert short_horizon.forward_burn != pytest.approx(long_horizon.forward_burn, rel=0.02)
