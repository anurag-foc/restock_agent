"""Phase 6's gate: the brief cannot be filled in wrongly.

Every test here corresponds to a failure that actually reached a PM, and each one is asserted
**structurally** — the brief does not contain a slot the wrong value could go into — rather than
by checking that an instruction is present. That distinction is the whole point. Each of these
had prose telling the model not to do it, in its instructions, at the time it did it:

- `QT-20260902-E85CF4` wrote "25 excess units, Rs 1,700 holding" against a real `excess_qty: 0`.
  A paragraph was added explaining that zero means no term. `QT-20260902-C85D0C` then wrote
  "4 units excess, Rs 272 holding" against the same real zero.
- Two attempts put the total first with the breakdown in a parenthetical. Both times the total
  was the ranking's internal `action_cost` and only the breakdown was real — `Rs 28,90,625` next
  to a breakdown summing to `Rs 5,16,120`, 5.6x out.
- Told to state "the real handling/freight cost, not zero", a run back-solved
  `Rs 17,280 (80 x Rs 216)` from `action_cost = exposure x 0.03`. `Rs 216` existed nowhere.
- `E85CF4` reported "no drift data for P1002" when the function returned a row, understating a
  wait by ~4 days.
- A report read "Transfer 150 units of P1015 from SUP-040" for what was correctly a purchase —
  a supplier is bought from, a warehouse is transferred from.
- One quote wrote a full report and zero part-lines because the model reached for a part *name*
  where a `PART_ID` was required.
"""

import re

import pytest

from agentic_restock import narration as N
from agentic_restock.detectors import findings as F


def _purchase_finding(*, excess_qty, holding, orderable=150, unit=3400.0):
    subtotal = orderable * unit
    return F.Finding(
        finding_type=F.STOCKOUT_RISK,
        subject_type=F.SUBJECT_PART_WAREHOUSE,
        subject_id="P1002@WH001",
        part_id="P1002",
        warehouse_id="WH001",
        supplier_id="SUP031",
        exposure=5_000_000.0,
        p_stockout=0.82,
        consequence=6_100_000.0,
        action_type=F.ACTION_PURCHASE,
        action_detail="buy 150 units of P1002 from SUP031 for WH001",
        action_cost=subtotal + holding,
        evidence={
            "purchase": {
                "orderable_qty": orderable,
                "effective_unit_cost": unit,
                "subtotal": subtotal,
                "excess_qty": excess_qty,
                "excess_months": 7.3 if excess_qty else 0.0,
                "excess_holding_cost": holding,
                "moq": 20,
                "pack_size": 1,
                "required_qty": 150,
            },
            "available_qty": 40,
            "on_hand_qty": 40,
            "in_transit_qty": 0,
            "forward_burn": 12.0,
            "days_of_cover": 3.3,
            "cover_threshold_days": 47.0,
        },
        assumptions_used=F.assumption_values(
            [F.ASSUMPTION_HOLDING_RATE, F.ASSUMPTION_SERVICE_LEVEL], criticality_class="B"
        ),
        suppression_key="P1002@WH001",
    )


def _transfer_finding():
    return F.Finding(
        finding_type=F.REDEPLOYMENT,
        subject_type=F.SUBJECT_PART_NETWORK,
        subject_id="P1015:WH026->WH003",
        part_id="P1015",
        warehouse_id="WH003",
        exposure=2_000_000.0,
        action_type=F.ACTION_TRANSFER,
        action_detail="transfer 80 units of P1015 from WH026 to WH003",
        action_cost=0.0,
        evidence={
            "transfer_qty": 80,
            "donor_warehouse_id": "WH026",
            "donor_available": 233,
            "donor_cover_after_days": 41.0,
            "freight_cost": 0.0,
        },
        assumptions_used=F.assumption_values([F.ASSUMPTION_SERVICE_LEVEL], criticality_class="B"),
        suppression_key="P1015@WH003",
    )


def _brief(findings):
    return N.build_brief(findings, {"considered": 188}).text


# --- the holding-cost fabrication ------------------------------------------


def test_a_zero_excess_produces_no_holding_clause_in_the_narrative_line():
    """Two prose fixes failed here. A clause that is absent cannot be filled with an invented
    figure; a clause that says "zero" can be, and twice was.

    Scoped to the IF line. `excess_holding_cost 0.0` DOES appear in the evidence dump, and
    should: a row of zeroes is data, not absence, and omitting it is what invites "no data
    returned". What must not exist is a sentence with a slot for a holding figure.
    """
    text = _brief([_purchase_finding(excess_qty=0, holding=0.0)])
    line = next(x for x in text.splitlines() if x.startswith("IF APPROVED AND WRONG:"))
    assert "holding" not in line.lower()
    assert "excess" not in line.lower()
    # ...while the evidence still records the real zero.
    assert "excess_holding_cost 0.0" in text


def test_a_real_excess_states_the_holding_cost_as_a_complete_trail():
    text = _brief([_purchase_finding(excess_qty=440, holding=63_119.0, orderable=500, unit=1700.0)])
    assert "440 excess units" in text
    assert "Rs 63,119" in text


def test_the_holding_figure_in_the_trail_is_the_one_from_the_evidence():
    """The IF line's holding term must be the value the evidence dump just printed — the failure
    was inventing a different one two lines below the real number."""
    finding = _purchase_finding(excess_qty=440, holding=63_119.0, orderable=500, unit=1700.0)
    text = _brief([finding])
    evidence_value = finding.evidence["purchase"]["excess_holding_cost"]
    assert N.format_inr(evidence_value) in text


# --- the total-first fabrication -------------------------------------------


def test_the_arithmetic_runs_left_to_right_with_the_total_last():
    """A slot for a total ahead of the arithmetic gets filled from whatever is nearest to hand.
    Removing the slot is what fixed it, after two prose attempts did not."""
    text = _brief([_purchase_finding(excess_qty=440, holding=63_119.0, orderable=500, unit=1700.0)])
    line = next(x for x in text.splitlines() if x.startswith("IF APPROVED AND WRONG:"))
    assert line.index("500 x") < line.index("=")
    assert line.rstrip().endswith("spent")


def test_the_trail_actually_sums():
    """The 5.6x discrepancy was a total that did not match its own breakdown."""
    text = _brief([_purchase_finding(excess_qty=440, holding=63_119.0, orderable=500, unit=1700.0)])
    line = next(x for x in text.splitlines() if x.startswith("IF APPROVED AND WRONG:"))
    # [unit_cost, subtotal, holding, total] -- the unit price leads the trail.
    figures = [int(m.replace(",", "")) for m in re.findall(r"Rs ([\d,]+)", line)]
    _unit, subtotal, holding, total = figures
    assert subtotal + holding == total


# --- action_cost presented as a price ---------------------------------------


def test_the_decision_value_line_never_prints_action_cost_as_a_figure():
    """It is a ranking input, not a price, and a PM cannot check it against any quote. Keeping it
    on screen was both the number the model reached for and an unverifiable claim."""
    finding = _purchase_finding(excess_qty=0, holding=0.0)
    text = _brief([finding])
    line = next(x for x in text.splitlines() if x.startswith("DECISION VALUE:"))
    assert N.format_inr(finding.action_cost) not in line
    assert "less" not in line
    assert "at risk" in line


def test_a_costless_action_states_one_figure_not_two_identical_ones():
    text = _brief([_transfer_finding()])
    line = next(x for x in text.splitlines() if x.startswith("DECISION VALUE:"))
    assert line.count("at risk") == 1
    assert "ranked after allowing" not in line


# --- the fabricated transfer cost -------------------------------------------


def test_a_transfer_with_no_freight_data_quotes_no_rupee_figure():
    """Told to state "the real handling cost, not zero", a run back-solved Rs 17,280 from
    action_cost. The line now states the actual downside instead."""
    text = _brief([_transfer_finding()])
    line = next(x for x in text.splitlines() if x.startswith("IF APPROVED AND WRONG:"))
    assert "Rs" not in line
    assert "41 days of cover" in line


def test_a_transfer_with_real_freight_data_may_quote_it():
    """The constraint was the absence of the column, not a rule about transfers. FREIGHT_COST is
    populated now, so a real figure is legitimate."""
    finding = _transfer_finding()
    finding.evidence["freight_cost"] = 1800.0
    text = _brief([finding])
    line = next(x for x in text.splitlines() if x.startswith("IF APPROVED AND WRONG:"))
    assert "Rs 1,800" in line


# --- "no data" about a call never made ---------------------------------------


def test_every_evidence_field_is_printed_verbatim():
    """A dump has no editorial latitude; a summary does. "No data" is a claim about a call you
    made, and it is wrong more often than right."""
    finding = _purchase_finding(excess_qty=440, holding=63_119.0)
    text = _brief([finding])
    for key in finding.evidence:
        if key == "purchase":
            continue
        assert key in text, key


def test_a_zero_valued_field_is_printed_rather_than_omitted():
    """A row of zeroes is data, not absence — omitting it is what invites "no data returned"."""
    finding = _purchase_finding(excess_qty=0, holding=0.0)
    text = _brief([finding])
    assert "in_transit_qty: 0" in text


# --- the wrong verb ---------------------------------------------------------


def test_a_purchase_recommendation_says_buy_and_names_a_supplier():
    """A supplier is bought from, a warehouse is transferred from. One report said "Transfer 150
    units of P1015 from SUP-040" for what OPTIONS CONSIDERED correctly called a purchase."""
    text = _brief([_purchase_finding(excess_qty=0, holding=0.0)])
    line = next(x for x in text.splitlines() if x.startswith("RECOMMENDATION:"))
    assert "buy" in line.lower()
    assert "transfer" not in line.lower()
    assert "SUP031" in line


def test_a_transfer_recommendation_says_transfer_and_names_a_warehouse():
    text = _brief([_transfer_finding()])
    line = next(x for x in text.splitlines() if x.startswith("RECOMMENDATION:"))
    assert "transfer" in line.lower()
    assert "buy" not in line.lower()
    assert "WH026" in line


def test_the_chosen_option_matches_the_recommendation_verb():
    for finding in (_purchase_finding(excess_qty=0, holding=0.0), _transfer_finding()):
        text = _brief([finding])
        chosen = next(x for x in text.splitlines() if "[CHOSEN]" in x)
        assert finding.action_type in chosen


# --- the part-name-instead-of-id failure -------------------------------------


def test_tool_arguments_are_pre_resolved_to_part_ids():
    """One quote wrote a full report and ZERO part-lines because the model reached for a part
    name. The header persisted, the quote became permanently "already exists", and the job
    reported SUCCESS."""
    brief = N.build_brief([_purchase_finding(excess_qty=0, holding=0.0)], {"considered": 10})
    assert brief.quote_lines[0]["item_id"] == "P1002"
    assert "do not substitute names" in brief.text
    assert "persist_quote" in brief.text


def test_a_run_with_nothing_to_persist_still_asks_for_a_notification():
    supplier_only = F.Finding(
        finding_type=F.LEADTIME_SIGNAL,
        subject_type=F.SUBJECT_SUPPLIER,
        subject_id="SUP010",
        supplier_id="SUP010",
        exposure=500_000.0,
        action_type=F.ACTION_RECALIBRATE,
        action_detail="SUP010: erratic",
    )
    brief = N.build_brief([supplier_only], {"considered": 10})
    assert "send_human_review" in brief.text


# --- disclosure -------------------------------------------------------------


def test_only_the_assumptions_a_finding_depends_on_are_disclosed():
    """A transfer buys nothing and holds nothing extra, so a holding rate has no business on it.
    Listing all of them every time is the same optional-slot failure in another guise."""
    transfer = _brief([_transfer_finding()])
    line = next(x for x in transfer.splitlines() if x.startswith("ASSUMPTIONS USED:"))
    assert "holding_rate" not in line
    assert "service_level" in line


def test_assumptions_are_labelled_as_policy_with_their_basis():
    text = _brief([_purchase_finding(excess_qty=0, holding=0.0)])
    line = next(x for x in text.splitlines() if x.startswith("ASSUMPTIONS USED:"))
    assert "policy" in line
    assert "14%" in line
    assert "cost_of_capital" in line


# --- the brief as a whole ---------------------------------------------------


def test_the_brief_tells_the_model_not_to_recompute():
    text = _brief([_purchase_finding(excess_qty=0, holding=0.0)])
    assert "do not recompute" in text


def test_only_the_why_now_line_is_left_to_write():
    """Everything else is pre-substituted. The model's job is prose, not arithmetic."""
    text = _brief([_purchase_finding(excess_qty=0, holding=0.0)])
    placeholders = [line for line in text.splitlines() if "<" in line and ">" in line]
    assert len(placeholders) == 1
    assert placeholders[0].startswith("WHY NOW:")


def test_items_are_numbered_for_the_teams_card_parser():
    """The card parses `## ACTION ITEM i of N` blocks; a report without them degrades to one item.

    Renamed from CANDIDATE for the PM-facing rework -- "candidate" describes the ranking's view of
    a row, not the thing a planner is being asked to do. Both the card splitter and the review
    app's parser accept either marker, because every quote written before the rename says
    CANDIDATE and must keep rendering.
    """
    text = _brief(
        [_purchase_finding(excess_qty=0, holding=0.0), _transfer_finding()]
    )
    assert "## ACTION ITEM 1 of 2" in text
    assert "## ACTION ITEM 2 of 2" in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [(640000, "Rs 6,40,000"), (100000, "Rs 1,00,000"), (999, "Rs 999"), (12345678, "Rs 1,23,45,678")],
)
def test_indian_digit_grouping(value, expected):
    assert N.format_inr(value).startswith(expected)
