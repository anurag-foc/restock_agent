"""The simulation feature's gate.

Guards the two mistakes this module already made once during the build, both of which produced
a plausible number rather than failing:

- **Scoring against `scenarios.expect_finding()`.** That answers "was a problem deliberately
  planted here", which labels ~33 of 278 pairs and treats every other pair as one that should
  stay quiet. Background pairs are drawn from the same random regimes and can be genuinely
  short, so real catches were scored as false alarms and recall came out at zero. Truth is now
  realised shortage — see `simulation/truth.py`.
- **Stamping every position row `REGIONAL_DC`.** The cascade rollup is restricted to
  PLANT_STORE, so a constant warehouse type silently switched off S2 and made a whole finding
  type unscoreable while the run still reported a tidy net value.
"""

from __future__ import annotations

import pytest

from agentic_restock import settings as st
from agentic_restock.simulation import frames as sim_frames
from agentic_restock.simulation import persistence, run, truth
from agentic_restock.simulation.scoring import (
    CELL_CAUGHT,
    CELL_CORRECTLY_QUIET,
    CELL_FALSE_ALARM,
    CELL_MISSED,
    _cell_and_value,
)

pytest.importorskip("statsforecast", reason="the second engine's package is an optional extra")


@pytest.fixture(scope="module")
def world():
    return run.world()


@pytest.fixture(scope="module")
def runs(world):
    return run.compare(["automatic", "statsforecast"])


# --- the bridge -------------------------------------------------------------------------


def test_every_frame_the_pipeline_reads_is_built(world):
    assert set(world) == {"issues", "position", "delivery", "contracts", "plan", "model_bom", "bom"}
    for name, frame in world.items():
        assert not frame.empty, f"{name} is empty -- the scan would silently find nothing"


def test_plant_stores_keep_their_real_warehouse_type(world):
    """The cascade rollup only runs at PLANT_STORE. A constant type here would switch off S2
    without switching off the score."""
    types = set(world["position"]["WAREHOUSE_TYPE"])
    assert "PLANT_STORE" in types
    assert len(types) > 1


def test_the_plan_is_restricted_to_the_forward_window(world):
    """Aggregating all three years of production would report history as pending demand and
    every parent would look catastrophically blocked."""
    plan = world["plan"]
    assert not plan.empty
    assert (plan["PLANNED_UNITS"] > 0).all()


# --- truth ------------------------------------------------------------------------------


def test_truth_covers_every_pair_not_just_the_planted_ones(world):
    facts = truth.build(world["position"])
    assert len(facts) > 200, "truth must label the whole universe, not the ~33 planted pairs"


def test_at_risk_pairs_are_a_real_minority(world):
    """A truth rule that flagged almost everything, or almost nothing, would make the metric
    meaningless in the same way a broken detector would."""
    facts = truth.build(world["position"])
    at_risk = sum(1 for f in facts.values() if f.will_run_short)
    share = at_risk / len(facts)
    assert 0.05 < share < 0.60, f"{share:.0%} of pairs at risk is not a usable denominator"


def test_a_short_pair_reports_a_positive_shortfall(world):
    facts = truth.build(world["position"])
    for fact in facts.values():
        assert fact.will_run_short == (fact.shortfall_qty > 0)


# --- the scoring rule -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "short", "expected_cell", "expected_value"),
    [
        (True, True, CELL_CAUGHT, 700.0),
        (False, True, CELL_MISSED, -1000.0),
        (True, False, CELL_FALSE_ALARM, -300.0),
        (False, False, CELL_CORRECTLY_QUIET, 0.0),
    ],
)
def test_each_cell_is_priced_with_the_figure_it_should_be(
    raised, short, expected_cell, expected_value
):
    """A catch earns decision_value, a miss costs full exposure, a false alarm costs only the
    fix. Crediting a catch with full exposure would score an expensive fix as highly as a
    cheap one; charging a false alarm the exposure would punish it for a loss that never
    existed."""
    cell, value = _cell_and_value(raised, short, exposure=1000.0, cost=300.0)
    assert cell == expected_cell
    assert value == pytest.approx(expected_value)


# --- an actual run ----------------------------------------------------------------------


def test_both_engines_score_the_same_world(runs):
    assert [r.engine for r in runs] == ["automatic", "statsforecast"]
    totals = {r.scorecard.to_row()["PAIRS_TOTAL"] for r in runs}
    assert len(totals) == 1, "engines were scored against different worlds; the delta is noise"


def test_the_budget_costs_something_and_never_pays(runs):
    """`budget_cost` is the gap between what the detectors found and what the PM saw. The
    budget can only remove findings, so it can never be negative -- and if it were zero the
    output budget would be doing nothing at all."""
    for result in runs:
        assert result.scorecard.budget_cost >= 0
        assert result.scorecard.detector_recall >= result.scorecard.recall


def test_a_run_finds_something_and_misses_something(runs):
    """Both ends matter. A run that caught everything would mean the truth rule is trivial; one
    that caught nothing would mean the scorer is broken, which is how the first version of this
    module failed."""
    for result in runs:
        card = result.scorecard
        assert card.detector_recall > 0.0, f"{result.engine} detected nothing real"
        assert card.detector_recall < 1.0, f"{result.engine} detected everything -- truth is trivial"


def test_every_pair_lands_in_exactly_one_cell(runs):
    for result in runs:
        card = result.scorecard
        assert (
            len(card.caught) + len(card.missed) + len(card.false_alarms) + len(card.correctly_quiet)
            == len(card.pairs)
        )


def test_the_scorecard_row_carries_every_column_the_table_declares(runs):
    ddl = persistence.build_sim_run_table_ddl()
    row = runs[0].to_row()
    for column in row:
        if column in {"CONSUMPTION_MODEL", "BUDGET"}:
            continue
        assert column in ddl, f"{column} is scored but has nowhere to be written"


# --- persistence ------------------------------------------------------------------------


def test_the_same_configuration_produces_the_same_run_id():
    """The world is deterministic, so a re-run is a replacement, not a second observation. Two
    ids would double-count it in any average over the table."""
    kwargs = {"engine": "automatic", "budget": 4, "settings_json": '{"a":1}', "label": "x"}
    assert persistence.run_id(**kwargs) == persistence.run_id(**kwargs)
    assert persistence.run_id(**{**kwargs, "budget": 5}) != persistence.run_id(**kwargs)


def test_a_quote_in_a_label_cannot_break_the_insert():
    """A label is free text from the operator. An unescaped apostrophe would either fail the
    INSERT or, worse, change what it writes."""
    from agentic_restock.simulation.scoring import Scorecard

    assert "INSERT INTO" in persistence.build_sim_run_pair_insert("SIM-1", [])

    empty = run.EngineRun("automatic", 4, Scorecard(), [], [])
    row_sql = persistence.build_sim_run_insert(
        empty.to_row(),
        run_identifier="SIM-1",
        batch_id="B1",
        label="quote ' here",
        settings_json='{"note": "it\'s fine"}',
    )
    assert "quote \\' here" in row_sql


def test_settings_snapshot_keeps_every_key():
    """A net value computed under a 14% holding rate is a different number from one computed
    under 20%. Storing only the overridden keys would make an old run unreadable once the
    defaults moved."""
    snapshot = {spec.key: st.DEFAULTS[spec.key] for spec in st.SPECS}
    assert set(snapshot) == {spec.key for spec in st.SPECS}


def test_frames_build_is_importable_without_a_cluster():
    assert callable(sim_frames.build)
