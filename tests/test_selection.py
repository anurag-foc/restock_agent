"""Phase 5's gate: suppression, duplicate collapse, and the output budget.

Guards a defect found during the build: diversifying across finding *type* is not enough,
because two types can describe one root cause. `CASCADE_BLOCK P0002: needs P0042` and
`STOCKOUT_RISK P0042: buy 4601 units` took two of four budget slots on the same problem.
"""

from datetime import date, timedelta

import pytest

from agentic_restock.detectors import findings as F
from agentic_restock.detectors import selection as SEL

TODAY = date(2026, 9, 5)


def _finding(finding_type, subject_id, dv, *, part=None, warehouse=None, evidence=None,
             action_type=F.ACTION_NONE, action_detail=""):
    """A finding with a chosen decision value (exposure minus a zero cost)."""
    return F.Finding(
        finding_type=finding_type,
        subject_type=F.SUBJECT_PART_WAREHOUSE,
        subject_id=subject_id,
        part_id=part,
        warehouse_id=warehouse,
        exposure=float(dv),
        action_type=action_type,
        action_detail=action_detail,
        evidence=evidence or {},
        suppression_key=subject_id,
    )


# --- suppression -----------------------------------------------------------


def test_a_fresh_pending_decision_suppresses_its_finding():
    """A PM who sees something already in their queue stops trusting the queue."""
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 1_000_000)]
    commitment = SEL.Commitment("P1@WH1", "PENDING_APPROVAL", TODAY - timedelta(days=1))
    assert SEL.apply_suppression(found, [commitment], as_of=TODAY) == []


def test_a_stale_pending_decision_re_surfaces_flagged():
    """Its exposure is still accruing while nothing happens."""
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 1_000_000)]
    commitment = SEL.Commitment("P1@WH1", "NEEDS_REVIEW", TODAY - timedelta(days=14))
    kept = SEL.apply_suppression(found, [commitment], as_of=TODAY)
    assert len(kept) == 1
    stalled = kept[0].evidence["stalled_commitment"]
    assert stalled["status"] == "NEEDS_REVIEW"
    assert stalled["age_days"] == 14


def test_an_approved_order_suppresses_for_its_own_lead_time():
    """Not a flat window: a 60-day part is not stalled at day 10, and a 20-day part is."""
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 1_000_000)]
    long_lead = SEL.Commitment(
        "P1@WH1", "APPROVED", TODAY - timedelta(days=40),
        decided_on=TODAY - timedelta(days=30), lead_days=60,
    )
    short_lead = SEL.Commitment(
        "P1@WH1", "APPROVED", TODAY - timedelta(days=40),
        decided_on=TODAY - timedelta(days=30), lead_days=20,
    )
    assert SEL.apply_suppression(found, [long_lead], as_of=TODAY) == []
    assert len(SEL.apply_suppression(found, [short_lead], as_of=TODAY)) == 1


def test_a_rejection_suppresses_permanently():
    """A closed decision stays closed — re-raising it is the failure this rule exists for."""
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 1_000_000)]
    ancient = SEL.Commitment("P1@WH1", "REJECTED", TODAY - timedelta(days=900),
                             decided_on=TODAY - timedelta(days=899))
    assert SEL.apply_suppression(found, [ancient], as_of=TODAY) == []


def test_a_completed_decision_does_not_suppress():
    """It is done; a fresh shortage on the same part is a new problem."""
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 1_000_000)]
    done = SEL.Commitment("P1@WH1", "COMPLETED", TODAY - timedelta(days=60),
                          decided_on=TODAY - timedelta(days=58))
    assert len(SEL.apply_suppression(found, [done], as_of=TODAY)) == 1


def test_suppression_is_keyed_at_each_scanner_own_grain():
    """A supplier-grain finding is suppressed by a decision about that supplier, not by one about
    a single part. The superseded design suppressed inside a (part, warehouse) WHERE clause, so a
    supplier-level signal could not be suppressed at all."""
    supplier_finding = F.Finding(
        finding_type=F.LEADTIME_SIGNAL, subject_type=F.SUBJECT_SUPPLIER,
        subject_id="SUP001", supplier_id="SUP001", exposure=500_000,
        suppression_key="supplier:SUP001",
    )
    part_decision = SEL.Commitment("P1@WH1", "PENDING_APPROVAL", TODAY)
    supplier_decision = SEL.Commitment("supplier:SUP001", "PENDING_APPROVAL", TODAY)
    assert len(SEL.apply_suppression([supplier_finding], [part_decision], as_of=TODAY)) == 1
    assert SEL.apply_suppression([supplier_finding], [supplier_decision], as_of=TODAY) == []


def test_findings_with_no_commitment_pass_through():
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 100), _finding(F.DEAD_CAPITAL, "P2@WH1", 200)]
    assert len(SEL.apply_suppression(found, [], as_of=TODAY)) == 2


# --- duplicate collapse ----------------------------------------------------


def test_a_cascade_subsumes_its_binding_children_shortages():
    """The defect this exists for: the same problem took two of four budget slots."""
    cascade = F.Finding(
        finding_type=F.CASCADE_BLOCK, subject_type=F.SUBJECT_PARENT_WAREHOUSE,
        subject_id="P0002@WH001", part_id="P0002", warehouse_id="WH001",
        exposure=109_560_000, evidence={"binding_children": ["P0042"]},
    )
    child = _finding(F.STOCKOUT_RISK, "P0042@WH001", 109_559_985, part="P0042", warehouse="WH001")
    collapsed = SEL.collapse_duplicates([cascade, child])
    assert len(collapsed) == 1
    assert collapsed[0].finding_type == F.CASCADE_BLOCK


def test_a_cascade_does_not_subsume_an_unrelated_part():
    cascade = F.Finding(
        finding_type=F.CASCADE_BLOCK, subject_type=F.SUBJECT_PARENT_WAREHOUSE,
        subject_id="P0002@WH001", part_id="P0002", warehouse_id="WH001",
        exposure=1_000_000, evidence={"binding_children": ["P0042"]},
    )
    other = _finding(F.STOCKOUT_RISK, "P0099@WH001", 500_000, part="P0099", warehouse="WH001")
    assert len(SEL.collapse_duplicates([cascade, other])) == 2


def test_transfer_and_purchase_on_one_pair_collapse_to_the_cheaper_fix():
    """Two options for one problem, not two problems."""
    transfer = _finding(
        F.REDEPLOYMENT, "P1:WH2->WH1", 900_000, part="P1", warehouse="WH1",
        action_type=F.ACTION_TRANSFER, action_detail="transfer 100 units",
    )
    purchase = _finding(
        F.STOCKOUT_RISK, "P1@WH1", 400_000, part="P1", warehouse="WH1",
        action_type=F.ACTION_PURCHASE, action_detail="buy 100 units",
    )
    collapsed = SEL.collapse_duplicates([transfer, purchase])
    assert len(collapsed) == 1
    assert collapsed[0].finding_type == F.REDEPLOYMENT


def test_the_rejected_alternative_is_recorded_not_discarded():
    """The report needs to show an option that was considered, rather than assert one existed."""
    transfer = _finding(
        F.REDEPLOYMENT, "P1:WH2->WH1", 900_000, part="P1", warehouse="WH1",
        action_type=F.ACTION_TRANSFER, action_detail="transfer 100 units",
    )
    purchase = _finding(
        F.STOCKOUT_RISK, "P1@WH1", 400_000, part="P1", warehouse="WH1",
        action_type=F.ACTION_PURCHASE, action_detail="buy 100 units",
    )
    kept = SEL.collapse_duplicates([transfer, purchase])[0]
    alternatives = kept.evidence["alternative_options"]
    assert len(alternatives) == 1
    assert alternatives[0]["action_type"] == F.ACTION_PURCHASE
    assert alternatives[0]["decision_value"] == 400_000


def test_supplier_grain_findings_are_never_collapsed_by_pair():
    supplier = F.Finding(
        finding_type=F.LEADTIME_SIGNAL, subject_type=F.SUBJECT_SUPPLIER,
        subject_id="SUP001", supplier_id="SUP001", exposure=100_000,
    )
    assert len(SEL.collapse_duplicates([supplier])) == 1


# --- selection -------------------------------------------------------------


def test_the_budget_is_hard():
    """Scarcity is the feature. 8,366 messages a week is the incumbent problem."""
    found = [_finding(F.STOCKOUT_RISK, f"P{i}@WH1", 1000 - i, part=f"P{i}", warehouse="WH1")
             for i in range(50)]
    assert len(SEL.select(found, budget=4)) <= 4


def test_one_scanner_cannot_take_the_whole_budget():
    """Exposure varies by orders of magnitude between types, so a global top-N would spend the
    whole budget on the same scanner every run — and the transfer opportunity, which the evidence
    base calls the strongest action available, would never be seen."""
    noisy = [_finding(F.STOCKOUT_RISK, f"P{i}@WH1", 10_000_000 - i, part=f"P{i}", warehouse="WH1")
             for i in range(20)]
    quiet = [_finding(F.REDEPLOYMENT, "PX:WH2->WH1", 5_000, part="PX", warehouse="WH1")]
    selected = SEL.select(noisy + quiet, budget=4)
    types = {f.finding_type for f in selected}
    assert F.REDEPLOYMENT in types
    assert sum(1 for f in selected if f.finding_type == F.STOCKOUT_RISK) <= SEL.MAX_PER_TYPE


def test_the_strongest_type_leads():
    found = [
        _finding(F.DEAD_CAPITAL, "P1@WH1", 100, part="P1", warehouse="WH1"),
        _finding(F.CASCADE_BLOCK, "P2@WH1", 10_000_000, part="P2", warehouse="WH1"),
    ]
    assert SEL.select(found, budget=2)[0].finding_type == F.CASCADE_BLOCK


def test_selection_is_ordered_by_decision_value():
    found = [
        _finding(F.STOCKOUT_RISK, "P1@WH1", 100, part="P1", warehouse="WH1"),
        _finding(F.DEAD_CAPITAL, "P2@WH1", 900, part="P2", warehouse="WH1"),
        _finding(F.REDEPLOYMENT, "P3:A->B", 500, part="P3", warehouse="WH1"),
    ]
    values = [f.decision_value for f in SEL.select(found, budget=3)]
    assert values == sorted(values, reverse=True)


def test_an_empty_or_zero_budget_selects_nothing():
    assert SEL.select([], budget=4) == []
    assert SEL.select([_finding(F.STOCKOUT_RISK, "P1@WH1", 100)], budget=0) == []


def test_fewer_findings_than_budget_returns_them_all():
    found = [_finding(F.STOCKOUT_RISK, "P1@WH1", 100, part="P1", warehouse="WH1")]
    assert len(SEL.select(found, budget=4)) == 1


def test_the_report_records_what_was_considered():
    """"We looked at 187 and raised 4" is only a claim if the 187 are written down."""
    found = [_finding(F.STOCKOUT_RISK, f"P{i}@WH1", 1000 - i, part=f"P{i}", warehouse="WH1")
             for i in range(30)]
    selected = SEL.select(found, budget=3)
    report = SEL.selection_report(found, selected)
    assert report["considered"] == 30
    assert report["selected"] == len(selected)
    assert report["considered_by_type"][F.STOCKOUT_RISK] == 30
    assert report["total_decision_value_available"] > report["total_decision_value_selected"]


def test_selection_is_deterministic():
    found = [
        _finding(F.STOCKOUT_RISK, "P1@WH1", 500, part="P1", warehouse="WH1"),
        _finding(F.DEAD_CAPITAL, "P2@WH1", 500, part="P2", warehouse="WH1"),
        _finding(F.REDEPLOYMENT, "P3:A->B", 500, part="P3", warehouse="WH1"),
    ]
    first = [f.subject_id for f in SEL.select(found, budget=2)]
    second = [f.subject_id for f in SEL.select(found, budget=2)]
    assert first == second


@pytest.mark.parametrize("budget", [1, 2, 3, 4, 8])
def test_selection_never_exceeds_the_budget(budget):
    found = [
        _finding(t, f"{t}:{i}", 1000 - i, part=f"P{i}", warehouse="WH1")
        for i, t in enumerate(F.ALL_FINDING_TYPES * 3)
    ]
    assert len(SEL.select(found, budget=budget)) <= budget


# --- learning from past decisions -------------------------------------------


def _rejected(key, *, decided_on, note=None, exposure=None):
    return SEL.Commitment(
        suppression_key=key,
        status="REJECTED",
        requested_on=decided_on,
        decided_on=decided_on,
        note=note,
        exposure_at_decision=exposure,
    )


def test_a_rejection_still_suppresses_when_nothing_has_changed():
    """A rejection is an answer, not a snooze. Time alone must not re-raise it."""
    finding = _finding("STOCKOUT_RISK", "P1@WH1", 1_000_000.0)
    kept = SEL.apply_suppression(
        [finding],
        [_rejected("P1@WH1", decided_on=date(2026, 1, 1), exposure=1_000_000.0)],
        as_of=date(2026, 9, 1),
    )
    assert kept == []


def test_a_rejection_re_surfaces_once_the_stakes_grow_enough():
    """The gap CLAUDE.md recorded for as long as the table had no EXPOSURE_AT_DECISION to
    compare against: re-surfacing was time-based only."""
    finding = _finding("STOCKOUT_RISK", "P1@WH1", 2_000_000.0)
    kept = SEL.apply_suppression(
        [finding],
        [_rejected("P1@WH1", decided_on=date(2026, 1, 1), exposure=1_000_000.0)],
        as_of=date(2026, 9, 1),
    )
    assert len(kept) == 1


def test_a_small_increase_is_not_enough_to_re_ask():
    finding = _finding("STOCKOUT_RISK", "P1@WH1", 1_200_000.0)
    kept = SEL.apply_suppression(
        [finding],
        [_rejected("P1@WH1", decided_on=date(2026, 1, 1), exposure=1_000_000.0)],
        as_of=date(2026, 9, 1),
    )
    assert kept == []


def test_a_rejection_with_no_recorded_exposure_still_suppresses():
    """Every line written before EXPOSURE_AT_DECISION existed has NULL there. Those must not
    all silently re-surface the moment this feature ships."""
    finding = _finding("STOCKOUT_RISK", "P1@WH1", 99_000_000.0)
    kept = SEL.apply_suppression(
        [finding],
        [_rejected("P1@WH1", decided_on=date(2026, 1, 1), exposure=None)],
        as_of=date(2026, 9, 1),
    )
    assert kept == []


def test_a_re_surfaced_finding_carries_the_note_the_pm_wrote():
    finding = _finding("STOCKOUT_RISK", "P1@WH1", 5_000_000.0)
    kept = SEL.apply_suppression(
        [finding],
        [
            _rejected(
                "P1@WH1",
                decided_on=date(2026, 1, 1),
                note="we dual-sourced this, ignore",
                exposure=1_000_000.0,
            )
        ],
        as_of=date(2026, 9, 1),
    )
    history = kept[0].evidence["prior_decisions"]
    assert history[0]["note"] == "we dual-sourced this, ignore"
    assert history[0]["decision"] == "REJECTED"
    assert history[0]["exposure_then"] == 1_000_000.0


# --- reason codes change what happens next ----------------------------------


def _rejected_with(reason, *, decided_on, exposure=1_000_000.0):
    return SEL.Commitment(
        suppression_key="P1@WH1",
        status="REJECTED",
        requested_on=decided_on,
        decided_on=decided_on,
        reason=reason,
        exposure_at_decision=exposure,
    )


def _still_raised(commitment, *, as_of, exposure=1_000_000.0):
    finding = _finding("STOCKOUT_RISK", "P1@WH1", exposure)
    return SEL.apply_suppression([finding], [commitment], as_of=as_of)


def test_cannot_act_now_comes_back_by_itself():
    """The whole reason the reason code exists. A budget freeze or a shutdown lifts without
    telling this system, and suppressing 'not right now' forever quietly deletes a real
    problem for the most ordinary reason there is."""
    decided = date(2026, 1, 1)
    assert _still_raised(
        _rejected_with(SEL.REASON_CANNOT_ACT_NOW, decided_on=decided),
        as_of=decided + timedelta(days=SEL.SNOOZE_DAYS + 1),
    )


def test_cannot_act_now_stays_quiet_inside_the_snooze_window():
    """It must not nag the same week it was deferred, or the code is just a slow re-raise."""
    decided = date(2026, 1, 1)
    assert (
        _still_raised(
            _rejected_with(SEL.REASON_CANNOT_ACT_NOW, decided_on=decided),
            as_of=decided + timedelta(days=SEL.SNOOZE_DAYS - 1),
        )
        == []
    )


def test_not_a_real_problem_does_not_come_back_on_a_timer():
    """The asymmetry: judging the FINDING closes it, judging the MOMENT does not."""
    decided = date(2026, 1, 1)
    assert (
        _still_raised(
            _rejected_with(SEL.REASON_NOT_A_PROBLEM, decided_on=decided),
            as_of=decided + timedelta(days=365),
        )
        == []
    )


def test_not_a_real_problem_still_returns_when_the_stakes_grow():
    decided = date(2026, 1, 1)
    assert _still_raised(
        _rejected_with(SEL.REASON_NOT_A_PROBLEM, decided_on=decided, exposure=1_000_000.0),
        as_of=decided + timedelta(days=30),
        exposure=3_000_000.0,
    )


def test_a_decision_with_no_reason_behaves_exactly_as_before():
    """Every line decided before this field existed has NULL in it. None of them may change
    behaviour the day this ships."""
    decided = date(2026, 1, 1)
    assert (
        _still_raised(
            _rejected_with(None, decided_on=decided),
            as_of=decided + timedelta(days=365),
        )
        == []
    )


def test_the_reason_reaches_the_report():
    decided = date(2026, 1, 1)
    kept = _still_raised(
        _rejected_with(SEL.REASON_CANNOT_ACT_NOW, decided_on=decided),
        as_of=decided + timedelta(days=SEL.SNOOZE_DAYS + 1),
    )
    assert kept[0].evidence["prior_decisions"][0]["reason"] == SEL.REASON_CANNOT_ACT_NOW
