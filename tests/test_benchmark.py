"""The trust benchmark is itself gated.

This file exists because the benchmark needed checking before it could be believed. On its first
run it reported 6 of 11, and three of those five "failures" were bugs in the checking code rather
than defects in the detectors — `supplier_id` on an S7 finding is the incumbent and not the
recommendation, and ranking a pair by exposure picks a different finding from the one selection
would pick. A benchmark that is wrong in our favour is worse than no benchmark; one that is wrong
against us wastes a day chasing a defect that is not there.

So the checks below assert the two properties that make the whole thing worth showing to anyone:
every planted problem is graded, and the grading is not vacuous.
"""

from __future__ import annotations

import pytest

from agentic_restock.detectors import findings as F
from agentic_restock.generation import scenarios
from agentic_restock.simulation import baseline, benchmark, persistence, run


@pytest.fixture(scope="module")
def world():
    return run.world()


@pytest.fixture(scope="module")
def graded(world):
    result = run.run_engine("automatic", world_frames=world)
    return benchmark.run(result.detected, result.selected, pair_count=len(world["position"]))


# --- coverage ---------------------------------------------------------------------------


def test_every_planted_finding_is_graded(graded):
    """The catalog declares eleven; a check that quietly stops covering one would raise the
    pass rate by dropping the hard case rather than by fixing it."""
    assert {c.finding_id for c in graded.checks} == set(scenarios.FINDINGS)
    assert graded.total == len(scenarios.FINDINGS)


def test_all_eight_detectors_are_exercised(graded):
    """The coverage headline the page leads on. If a scanner has no planted case behind it,
    "8 of 8 kinds of problem" is an assertion rather than a measurement."""
    exercised = {c.finding_type for c in graded.checks if c.finding_type in F.ALL_FINDING_TYPES}
    assert exercised == set(F.ALL_FINDING_TYPES), (
        f"no planted case for {set(F.ALL_FINDING_TYPES) - exercised}"
    )


def test_the_silence_tests_are_present_and_are_a_real_minority(graded):
    """Two negatives out of eleven. A benchmark of positives only scores highest for a detector
    that fires on everything, which is the failure this product exists to remove."""
    negatives = graded.negatives
    assert len(negatives) == 2
    assert {c.finding_id for c in negatives} == {"F9", "F10"}
    assert len(graded.positives) == graded.total - 2


# --- the grading is not vacuous ---------------------------------------------------------


def test_a_check_that_passes_says_what_it_checked(graded):
    """Every row on the page names a real part, warehouse or supplier so a viewer can verify it
    by hand. A pass with an empty subject is unfalsifiable and must not reach the page."""
    for check in graded.checks:
        assert check.detail.strip(), f"{check.finding_id} passed with no explanation"
        if not check.negative:
            assert check.subject.strip(), f"{check.finding_id} names no subject"


def test_the_benchmark_fails_when_the_pipeline_finds_nothing(world):
    """The load-bearing one. A grader that returns PASS on an empty finding list is measuring
    its own optimism, and every positive row on the page would be worthless."""
    empty = benchmark.run([], [], pair_count=len(world["position"]))
    assert empty.passed <= len(empty.negatives), (
        "a run that found nothing scored a positive check as passed"
    )
    assert empty.types_covered == set()
    for check in empty.positives:
        assert check.result == benchmark.FAIL


def test_silence_tests_pass_when_nothing_was_raised(world):
    """The mirror of the above: with no findings at all, the two quiet checks must pass. If they
    failed here the negatives would not be measuring silence."""
    empty = benchmark.run([], [], pair_count=len(world["position"]))
    assert all(c.result == benchmark.PASS for c in empty.negatives)


# --- what it actually scores on the shipped dataset -------------------------------------


def test_the_shipped_dataset_passes_every_check(graded):
    """The number the page shows. A regression here is a real detector regression, and it is
    worth failing the suite over rather than discovering in a demo."""
    failures = [(c.finding_id, c.detail) for c in graded.checks if c.result != benchmark.PASS]
    assert not failures, f"benchmark regressed: {failures}"


def test_the_transfer_check_rejects_a_donor_it_would_break(graded):
    """F1's substance. The detector must not relieve the receiver by pushing the donor over the
    threshold at which the donor becomes the next finding -- 'moving a shortage is not a fix'."""
    f1 = next(c for c in graded.checks if c.finding_id == "F1")
    assert f1.result == benchmark.PASS
    assert scenarios.FINDINGS["F1"]["decoy_donor"] not in f1.subject


# --- persistence parity -----------------------------------------------------------------


def test_every_benchmark_column_written_exists_in_the_table(graded):
    """Same gate as `sim_run` has, and for the same reason: the INSERT and the DDL are written
    in two places and drift silently until a job fails at 2am."""
    ddl = persistence.build_sim_benchmark_table_ddl()
    insert = persistence.build_sim_benchmark_insert(
        graded.checks, batch_id="TEST", label="test"
    )
    columns = insert.split("(", 1)[1].split(")", 1)[0]
    for column in (c.strip() for c in columns.split(",")):
        assert column in ddl, f"{column} is written but has nowhere to go"


def test_a_label_with_a_quote_does_not_break_the_insert(graded):
    """Labels are operator free text and reach SQL as a literal."""
    insert = persistence.build_sim_benchmark_insert(
        graded.checks, batch_id="TEST", label="Rahul's run"
    )
    assert "\\'" in insert
    assert "Rahul's run" not in insert


# --- the incumbent arms the page compares against ---------------------------------------


def test_both_incumbent_rules_run_and_differ(world):
    """The page has no origin without them. If the two rules produced identical output, offering
    both would be noise -- the reorder point must see more than the bare safety-stock breach."""
    arms = baseline.run_all(world_frames=world)
    assert [a.engine for a in arms] == list(baseline.RULES)
    by_rule = {a.engine: a for a in arms}
    crude = by_rule[baseline.RULE_SAFETY_STOCK]
    textbook = by_rule[baseline.RULE_REORDER_POINT]
    assert len(textbook.detected) > len(crude.detected)
    assert all(a.budget == 0 for a in arms), "an incumbent rule emits every breach, uncapped"


def test_the_incumbent_is_scored_on_the_same_universe_as_ours(world):
    """Both arms must face the same denominator, or the comparison on the page is between two
    different questions."""
    arms = baseline.run_all(world_frames=world)
    ours = run.run_engine("automatic", world_frames=world)
    at_risk = {a.scorecard.to_row()["PAIRS_AT_RISK"] for a in arms}
    at_risk.add(ours.scorecard.to_row()["PAIRS_AT_RISK"])
    assert len(at_risk) == 1, f"arms scored against different denominators: {at_risk}"


# --- what the page shows as the run's actual output --------------------------------------


def test_every_surfaced_finding_column_exists_in_the_table(world):
    """`sim_selection` is written and read in two places; the same drift gate as the others."""
    result = run.run_engine("automatic", world_frames=world)
    ddl = persistence.build_sim_selection_table_ddl()
    insert = persistence.build_sim_selection_insert(
        result.selected, run_identifier="TEST", batch_id="B", engine="automatic"
    )
    columns = insert.split("(", 1)[1].split(")", 1)[0]
    for column in (c.strip() for c in columns.split(",")):
        assert column in ddl, f"{column} is written but has nowhere to go"


def test_an_empty_selection_writes_no_sql(world):
    """A run that surfaced nothing must not emit `VALUES ` with an empty tail, which is a
    syntax error that would fail the job after the scoring had already succeeded."""
    assert (
        persistence.build_sim_selection_insert(
            [], run_identifier="TEST", batch_id="B", engine="automatic"
        )
        == ""
    )


def test_what_reaches_the_pm_spans_more_than_one_kind_of_problem(world):
    """The claim the page's output panel makes. If the budget only ever surfaced shortages, the
    'eight kinds of problem' argument would be true of the detectors and false of the product."""
    result = run.run_engine("automatic", world_frames=world)
    kinds = {f.finding_type for f in result.selected}
    assert len(kinds) > 1, f"the budget surfaced only {kinds}"


def test_every_surfaced_finding_carries_its_own_arithmetic(world):
    """Each figure on the page is shown with the derivation underneath it. A finding with no
    basis puts an unsourced rupee number in front of a client."""
    result = run.run_engine("automatic", world_frames=world)
    for finding in result.selected:
        assert finding.exposure_basis.strip(), f"{finding.subject_id} has no exposure basis"
        assert finding.action_detail.strip(), f"{finding.subject_id} has no recommendation"
