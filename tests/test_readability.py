"""Names in the prose, and the check that the model did not invent a figure.

The verification half matters more than it looks. Before it existed, nothing compared the
model's sentences against the numbers it was handed -- the pipeline verified that the tools were
*called*, not that the prose was true. That is the same "the rule was in its instructions"
posture that produced every fabrication on record.
"""

import pytest

from agentic_restock.readability import (
    EMPTY_NAMES,
    NameBook,
    allowed_figures,
    humanise,
    numbers_in,
    verify_prose,
    verify_report,
)

NAMES = NameBook(
    parts={"P0021": "Caliper Group", "P0074": "Compressor Clutch"},
    warehouses={"WH001": "Gurgaon Plant Store", "WH002": "Manesar Plant Store"},
    suppliers={"SUP015": "Palar Rubber Industries"},
)


# --- names -----------------------------------------------------------------


def test_keys_become_names_keeping_the_key_for_cross_reference():
    # The real line from a live quote, which no reader could picture.
    out = humanise("transfer 2582 units of P0021 from WH002 to WH001", NAMES)
    assert out == (
        "transfer 2582 units of Caliper Group (P0021) "
        "from Manesar Plant Store (WH002) to Gurgaon Plant Store (WH001)"
    )


def test_an_unknown_key_is_left_exactly_as_it_was():
    # A part added since the name book was built must degrade to the ID, not to a blank.
    assert humanise("review P9999 at WH001", NAMES) == "review P9999 at Gurgaon Plant Store (WH001)"


def test_empty_name_book_changes_nothing():
    text = "transfer 2582 units of P0021 from WH002 to WH001"
    assert humanise(text, EMPTY_NAMES) == text


def test_humanise_is_idempotent():
    once = humanise("move P0021 to WH001", NAMES)
    assert humanise(once, NAMES) == once, "running twice must not nest the brackets"


def test_suppliers_resolve_too():
    assert "Palar Rubber Industries (SUP015)" in humanise("SUP015: consistently late", NAMES)


# --- number extraction -----------------------------------------------------


def test_indian_grouped_figures_are_read_as_one_number():
    assert numbers_in("Rs 2,95,83,118 at stake") == [29583118.0]


def test_decimals_and_percentages_are_picked_up():
    assert numbers_in("risk falls from 100% to 4.9%") == [100.0, 4.9]


# --- verification ----------------------------------------------------------


EVIDENCE = {
    "transfer_qty": 2582,
    "receiver_consequence": 29583117.84,
    "donor_available": 7491,
    "donor_cover_after_days": 48.7,
    "receiver_risk_after": 0.049,
}


def test_prose_built_only_from_the_evidence_passes():
    verdict = verify_prose(
        "Moving 2582 units leaves the donor 7491 on hand and 49 days of cover.", EVIDENCE
    )
    assert verdict.ok, verdict.reason


def test_a_figure_that_was_never_computed_is_caught():
    """The exact failure on record: told to state a real handling cost, the model back-solved
    `Rs 17,280 (80 x Rs 216)` from a percentage. Rs 216 existed nowhere."""
    verdict = verify_prose("Handling costs Rs 17,280 (80 x Rs 216).", EVIDENCE)
    assert not verdict.ok
    assert 216.0 in verdict.invented


def test_rounding_and_rescaling_a_real_figure_is_allowed():
    # A gate that fires on legitimate rounding gets switched off within a week, which is worse
    # than no gate -- so 2.96 crore, 49 days and 5% all have to pass.
    for phrasing in (
        "about Rs 2.96 crore is at stake",
        "the donor keeps 49 days of cover",
        "risk falls to 5%",
    ):
        assert verify_prose(phrasing, EVIDENCE).ok, phrasing


def test_magnitude_words_are_refused_because_they_cannot_be_checked():
    verdict = verify_prose("Demand has doubled since last year.", EVIDENCE)
    assert not verdict.ok
    assert "magnitude" in verdict.reason


def test_small_counts_and_ordinals_are_not_treated_as_claims():
    assert verify_prose("This is 1 of 4 items, affecting 2 sites.", EVIDENCE).ok


def test_extra_figures_can_be_allowed_explicitly():
    # Figures that live on the finding rather than in the evidence dict -- exposure, decision
    # value -- are legitimate for the model to quote and are passed in by the caller.
    assert not verify_prose("worth Rs 26,828,618", EVIDENCE).ok
    assert verify_prose("worth Rs 26,828,618", EVIDENCE, extra=[26828618.0]).ok


def test_allowed_figures_reads_numbers_out_of_string_evidence():
    # Several scanners put figures inside text fields (`affected_parts`, basis strings).
    allowed = allowed_figures({"note": "40 observations over 60 days"})
    assert 40.0 in allowed
    assert 60.0 in allowed


@pytest.mark.parametrize("empty", ["", None])
def test_empty_prose_is_not_a_failure(empty):
    assert verify_prose(empty, EVIDENCE).ok


# --- whole-report checking -------------------------------------------------


class _Finding:
    """Minimal stand-in: verify_report only reads these five attributes."""

    def __init__(self, evidence, exposure=0.0, decision_value=0.0, consequence=0.0,
                 action_cost=0.0):
        self.evidence = evidence
        self.exposure = exposure
        self.decision_value = decision_value
        self.consequence = consequence
        self.action_cost = action_cost


REPORT = """Some preamble the model wrote.

## ACTION ITEM 1 of 2

RECOMMENDATION: move 2582 units
WHY NOW: 2582 units are short and the donor keeps 7491.
IF YOU DO NOTHING: it runs out.

## ACTION ITEM 2 of 2

RECOMMENDATION: review stock
WHY NOW: handling costs Rs 17,280 (80 x Rs 216).
"""


def test_a_clean_report_reports_nothing():
    findings = [_Finding({"transfer_qty": 2582, "donor_available": 7491}), _Finding({"qty": 80})]
    problems = verify_report(REPORT.replace("handling costs Rs 17,280 (80 x Rs 216)",
                                            "80 units are involved"), findings)
    assert problems == []


def test_the_invented_figure_is_caught_and_the_item_named():
    findings = [_Finding({"transfer_qty": 2582, "donor_available": 7491}), _Finding({"qty": 80})]
    problems = verify_report(REPORT, findings)
    assert len(problems) == 1
    index, verdict = problems[0]
    assert index == 2, "must name which item was wrong, not just that something was"
    assert 216.0 in verdict.invented


def test_a_wrong_item_count_is_reported_rather_than_mispaired():
    # Pairing positionally against the wrong number of items would check each sentence against
    # someone else's evidence and produce confident nonsense.
    problems = verify_report(REPORT, [_Finding({"transfer_qty": 2582})])
    assert len(problems) == 1
    assert "2 items, 1 were raised" in problems[0][1].reason


def test_figures_from_the_finding_itself_are_allowed():
    # exposure / decision value are quotable but live on the finding, not in the evidence dict.
    report = "## ACTION ITEM 1 of 1\nWHY NOW: worth Rs 26,828,618 in total.\n"
    assert verify_report(report, [_Finding({}, exposure=26828618.0)]) == []
