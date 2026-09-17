"""Plain-English reasoning is itself gated.

The benchmark chart's whole trust argument is "don't take the count on faith — here's the real
part and the real numbers." A sentence that silently degrades to "None a day" is worse than no
sentence at all: it looks sourced and isn't. This file exists because that exact failure
happened once already, on the ERP arm's differently-shaped evidence -- see `reasoning.py`'s
`_round` docstring.
"""

from __future__ import annotations

import pytest

from agentic_restock.detectors import findings as F
from agentic_restock.simulation import baseline, run
from agentic_restock.simulation import reasoning as R


@pytest.fixture(scope="module")
def world():
    return run.world()


@pytest.fixture(scope="module")
def our_findings(world):
    return run.run_engine("automatic", world_frames=world).detected


@pytest.fixture(scope="module")
def erp_findings(world):
    cfg = run._settings_for("automatic", None, None)
    measures = run.measure(cfg, world)
    truth = baseline.truth_for(measures, world)
    engine_run, _ = baseline.run_erp(baseline.RULE_REORDER_POINT, measures, truth)
    return engine_run.detected


def test_every_finding_type_has_an_explainer():
    """A ninth scanner added later without a matching explainer would silently fall back to
    the terser action_detail everywhere -- worth knowing, not worth discovering on the page."""
    assert set(R.PLAIN_NAME) == set(F.ALL_FINDING_TYPES)


def test_a_real_example_of_every_type_explains_without_a_missing_field(our_findings):
    """Every one of the eight bars on the chart must expand into a real sentence. A silent
    'None' anywhere here is the exact failure this module exists to prevent."""
    for finding_type in F.ALL_FINDING_TYPES:
        example = R.best_example(our_findings, finding_type)
        assert example is not None, f"no planted case produced a {finding_type} finding"
        text = R.explain(example)
        assert text.strip()
        assert "None" not in text, f"{finding_type}: {text!r}"


def test_the_erp_arms_differently_shaped_evidence_falls_back_cleanly(erp_findings):
    """The regression case. The ERP arm's STOCKOUT_RISK evidence carries
    naive_daily_consumption/contracted_lead_days, not forward_burn/mu_lead_days -- the shape
    S1's own findings carry. explain() must fall back to action_detail rather than print
    'None a day' into a stored sentence."""
    assert erp_findings, "fixture produced no findings to check"
    for finding in erp_findings:
        text = R.explain(finding)
        assert "None" not in text, f"{finding.subject_id}: {text!r}"


def test_type_summary_rows_never_contains_a_stray_none(our_findings, erp_findings):
    for findings in (our_findings, erp_findings):
        for row in R.type_summary_rows(findings):
            assert "None" not in row["example_reasoning"], row
            assert row["example_subject"], f"{row['finding_type']} has no example subject"
            assert row["count"] > 0


def test_erp_findings_explain_the_rule_not_the_burn_estimate(erp_findings):
    """The regression this feature exists to fix: a lone count with no explanation. The ERP
    explainer must name its own trigger (safety stock, or reorder point) and must not describe
    itself using our corrected burn/lead-time vocabulary, which it does not have."""
    for finding in erp_findings:
        text = R.explain(finding)
        assert "reorder point" in text or "safety stock" in text
        assert "days of stock" not in text, "used our burn-based phrasing, not the ERP's own rule"


def test_type_summary_rows_omits_types_this_arm_never_produces(erp_findings):
    """A reorder-point rule only ever emits STOCKOUT_RISK. A zero-count row for the other seven
    would understate how structurally narrow it is -- 'no concept of this problem' should read
    as an absent bar, not a bar at zero."""
    rows = R.type_summary_rows(erp_findings)
    assert {r["finding_type"] for r in rows} == {F.STOCKOUT_RISK}


def test_accuracy_note_only_appears_for_shortage_addressing_types(our_findings, world):
    """The precision claim -- the one that actually answers 'how often is this right' -- must
    only be printed where a real ground truth exists to check it against. Printing one for dead
    capital or a supplier switch would be inventing a number the generator never labelled."""
    from agentic_restock.detectors import findings as F

    result = run.run_engine("automatic", world_frames=world)
    rows = R.type_summary_rows(result.detected, truth=result.truth)
    by_type = {r["finding_type"]: r for r in rows}
    for finding_type in F.ALL_FINDING_TYPES:
        row = by_type.get(finding_type)
        if row is None:
            continue
        if finding_type in {F.STOCKOUT_RISK, F.CASCADE_BLOCK, F.REDEPLOYMENT}:
            assert row["accuracy_note"], f"{finding_type} has ground truth but no note"
        else:
            assert row["accuracy_note"] == "", f"{finding_type} should have no accuracy claim"


def test_accuracy_note_discloses_a_partial_denominator(our_findings, world):
    """REDEPLOYMENT reaches in-house parts truth cannot judge, so `checked` is routinely well
    below `count`. The note must say both numbers when that happens -- a bare 'right 11 of 11'
    with no mention of the other 22 reads as a suspiciously perfect number, not an honest one."""
    result = run.run_engine("automatic", world_frames=world)
    rows = R.type_summary_rows(result.detected, truth=result.truth)
    row = next(r for r in rows if r["finding_type"] == "REDEPLOYMENT")
    assert row["count"] > 11  # more were found than could be checked, on this dataset
    assert "we could check" in row["accuracy_note"].lower()


def test_the_erp_arm_gets_its_own_accuracy_note_too(erp_findings, world):
    """Fairness, not just ours: the incumbent's own hit rate is checkable the same way, and
    withholding it while showing ours would be the exact asymmetry that makes a benchmark read
    as one-sided."""
    rows = R.type_summary_rows(erp_findings, truth=erp_findings and _erp_truth(world))
    assert rows[0]["accuracy_note"], "the ERP arm's STOCKOUT_RISK row has no accuracy note"


def _erp_truth(world):
    cfg = run._settings_for("automatic", None, None)
    measures = run.measure(cfg, world)
    return baseline.truth_for(measures, world)


def test_a_run_with_no_truth_passed_produces_no_accuracy_claims(our_findings):
    """Calling `type_summary_rows` without `truth` must degrade to silence, not a crash and not
    a fabricated number -- this is the signature every other caller used before this feature
    existed, and it must stay safe to call that way."""
    rows = R.type_summary_rows(our_findings)
    assert all(r["accuracy_note"] == "" for r in rows)


def test_explain_does_not_raise_on_a_finding_with_empty_evidence():
    """Defence in depth: a scanner that ships with a thinner evidence dict than today's must
    degrade to the finding's own action_detail, not crash the page."""
    bare = F.Finding(
        finding_type=F.STOCKOUT_RISK,
        subject_type=F.SUBJECT_PART_WAREHOUSE,
        subject_id="P0000@WH000",
        part_id="P0000",
        warehouse_id="WH000",
        action_detail="buy some units",
        evidence={},
    )
    assert R.explain(bare) == "buy some units"
