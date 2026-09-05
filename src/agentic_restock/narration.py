"""Assemble the brief the Supervisor writes from.

The brief carries three things, and the third is the design decision:

1. **The evidence**, as a verbatim field dump. Every figure the report needs, already measured.
2. **The output skeleton** — section by section, so the format does not live in agent
   instructions where it competes for attention with everything else.
3. **The arithmetic, already performed.** Not "state the holding cost" but
   `440 excess x Rs 1,700 = Rs 7,48,000 held for 220 days (0.60 yr) x 14%/yr = Rs 63,119`,
   pre-formed. The model writes the sentence around a figure it cannot recompute.

That third point is the whole approach. The three fabrications on record were not caused by the
model failing to *see* a rule -- the rule was in its instructions each time. They happened
because it was a rule rather than a constraint. What worked was structural: removing the
headline slot so there was no place to put a total before the arithmetic; making evidence a
verbatim dump so there was no editorial latitude; fixing the quantity input so there was no
discretion. Pre-substituting the values is the same move applied to the whole report -- there is
no slot to fill, so there is nothing to fill wrongly.

The tool arguments are pre-assembled for the same reason. One live quote wrote a full report and
**zero** part-lines because the model reached for a part *name* where a `PART_ID` was required;
the header persisted, the quote became permanently "already exists", and the job reported
SUCCESS. Handing over exact arguments removes that entirely.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_restock.detectors import findings as F

# Plain-English rendering of the reason codes. The PM chose from a dropdown; the report should
# read back what they meant, not the enum they picked.
REASON_LABELS = {
    "NOT_A_PROBLEM": "not a real problem",
    "ALREADY_HANDLED": "already handled",
    "CANNOT_ACT_NOW": "could not act at the time",
    "NUMBERS_WRONG": "numbers looked wrong",
    "OTHER": "other",
}

URGENCY_BY_SHARE = ((0.5, "CRITICAL"), (0.2, "HIGH"), (0.05, "MEDIUM"))
DEFAULT_URGENCY = "LOW"


def format_inr(value: float, *, paise: bool = False) -> str:
    """Indian digit grouping: 6,40,000 rather than 640,000.

    The reports are read by an Indian manufacturer's planners, and 2,45,00,000 is legible to them
    in a way 24,500,000 is not.
    """
    negative = value < 0
    whole = abs(float(value))
    fraction = f"{whole - int(whole):.2f}"[1:] if paise else ""
    digits = str(int(whole))

    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])

    return f"{'-' if negative else ''}Rs {grouped}{fraction}"


def format_scale(value: float) -> str:
    """A rupee figure with its crore/lakh scale, since the magnitudes span both."""
    absolute = abs(float(value))
    if absolute >= 1_00_00_000:
        return f"{format_inr(value)} ({absolute / 1_00_00_000:.2f} crore)"
    if absolute >= 1_00_000:
        return f"{format_inr(value)} ({absolute / 1_00_000:.2f} lakh)"
    return format_inr(value)


def _urgency(finding: F.Finding, total_decision_value: float) -> str:
    if total_decision_value <= 0:
        return DEFAULT_URGENCY
    share = finding.decision_value / total_decision_value
    for threshold, label in URGENCY_BY_SHARE:
        if share >= threshold:
            return label
    return DEFAULT_URGENCY


# ---------------------------------------------------------------------------
# Pre-formed arithmetic trails
# ---------------------------------------------------------------------------


def purchase_arithmetic(evidence: dict) -> str | None:
    """The `IF APPROVED AND WRONG` trail for a buy: left to right, **total last**.

    Total last is not a style preference. Two earlier attempts put the total first with the
    breakdown in a parenthetical, and both times the total was the ranking's internal
    `action_cost` while only the breakdown was real -- once 5.6x out. A template with no headline
    slot to fill ahead of the arithmetic is what fixed it.
    """
    purchase = evidence.get("purchase")
    if not purchase:
        return None

    qty = purchase.get("orderable_qty", 0)
    unit = purchase.get("effective_unit_cost", 0.0)
    subtotal = purchase.get("subtotal", 0.0)
    holding = purchase.get("excess_holding_cost", 0.0)

    trail = f"{qty} x {format_inr(unit, paise=True)} = {format_inr(subtotal)}"
    if holding > 0:
        excess = purchase.get("excess_qty", 0)
        months = purchase.get("excess_months", 0.0)
        trail += (
            f" plus {format_inr(holding)} to hold {excess} excess units "
            f"for {months:.1f} months = {format_inr(subtotal + holding)} spent"
        )
    else:
        # No holding term because there is no excess. Two prose fixes failed to stop the model
        # filling this slot anyway -- it invented 'Rs 1,700 holding' against a real zero, twice.
        # Omitting the clause entirely leaves nothing to fill.
        trail += " spent"
    return trail


def transfer_downside(evidence: dict) -> str:
    """A transfer's downside, stated in words plus the one real cost that exists.

    Moving owned stock spends no purchase price. Freight is now a populated column, so it can be
    quoted -- but the substantive downside is what the donor gives up, and that is stated rather
    than converted into a rupee figure. Told once to state "the real handling cost, not zero",
    the model back-solved `Rs 17,280 (80 x Rs 216)` from `action_cost = exposure x 0.03`. Rs 216
    existed nowhere.
    """
    cover = evidence.get("donor_cover_after_days")
    donor = evidence.get("donor_available")
    freight = evidence.get("freight_cost", 0.0)

    parts = []
    if freight and freight > 0:
        parts.append(f"freight of {format_inr(freight)}")
    if cover is not None:
        parts.append(
            f"the donor is left with {cover:.0f} days of cover"
            + (f" from {donor} units" if donor is not None else "")
        )
    return "; ".join(parts) if parts else "no purchase cost — owned stock is moved, not bought"


def dead_capital_arithmetic(evidence: dict) -> str:
    qty = evidence.get("on_hand_qty", 0)
    unit = evidence.get("unit_cost", 0.0)
    trapped = evidence.get("trapped_value", 0.0)
    carry = evidence.get("annual_carrying_cost", 0.0)
    return (
        f"{qty} x {format_inr(unit, paise=True)} = {format_inr(trapped)} idle, "
        f"costing {format_inr(carry)} a year to hold"
    )


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

_TEMPLATE = """\
## CANDIDATE {index} of {total}

RECOMMENDATION: {recommendation}
WHY NOW: <one or two sentences, from the evidence below. No figure that is not printed there.>
OPTIONS CONSIDERED:
{options}
EVIDENCE:
{evidence_lines}{prior_decisions}
IF APPROVED AND WRONG: {if_wrong}
DECISION VALUE: {decision_value}
ASSUMPTIONS USED: {assumptions}
"""


@dataclass
class Brief:
    """What gets sent to the Supervisor, and the tool arguments it should pass through."""

    text: str
    quote_lines: list[dict]
    unpersistable: list[F.Finding]


def _evidence_lines(finding: F.Finding) -> str:
    """A verbatim field dump, every field in order.

    A dump has no editorial latitude; a summary does. The one function that reported
    `no data returned` while genuinely returning a row understated a wait by four days, and the
    general rule that followed is that "no data" is a claim about a call you made and is wrong
    more often than right.
    """
    lines: list[str] = []
    for key, value in finding.evidence.items():
        # Rendered as its own PREVIOUSLY DECIDED section, where the PM's own words are quoted
        # exactly. Dumping the raw dicts here as well would print it twice, the second time
        # unreadably.
        if key == "prior_decisions":
            continue
        if isinstance(value, dict):
            inner = ", ".join(f"{k} {v}" for k, v in value.items())
            lines.append(f"  {key}: {inner}")
        elif isinstance(value, list):
            lines.append(f"  {key}: {', '.join(str(v) for v in value) or '(none)'}")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _options_block(finding: F.Finding) -> str:
    """The chosen action and any alternative, with the chosen cost already computed."""
    chosen_cost = (
        transfer_downside(finding.evidence)
        if finding.action_type == F.ACTION_TRANSFER
        else format_scale(finding.action_cost)
        if finding.action_cost > 0
        else "no direct cost"
    )
    lines = [f"  [CHOSEN] {finding.action_type}: {finding.action_detail} — cost {chosen_cost}"]
    for alternative in finding.evidence.get("alternative_options", []):
        lines.append(
            f"  [NOT CHOSEN] {alternative['action_type']}: {alternative['action_detail']} — "
            f"cost {format_scale(alternative['action_cost'])}, "
            f"decision value {format_scale(alternative['decision_value'])}"
        )
    if len(lines) == 1:
        lines.append("  [NOT CHOSEN] no other option was available for this subject")
    return "\n".join(lines)


def _if_wrong(finding: F.Finding) -> str:
    if finding.action_type == F.ACTION_TRANSFER:
        return transfer_downside(finding.evidence)
    if finding.finding_type == F.DEAD_CAPITAL:
        return dead_capital_arithmetic(finding.evidence)
    trail = purchase_arithmetic(finding.evidence)
    if trail:
        return trail
    if finding.action_cost > 0:
        return f"{format_scale(finding.action_cost)} committed"
    return "no spend is committed by this action"


def _decision_value_line(finding: F.Finding) -> str:
    """Decision value with what is at risk — and NOT with `action_cost` as a rupee figure.

    That line used to read `Rs <dv> (exposure Rs <e> less Rs <action_cost> to act)`. Keeping
    `action_cost` on screen was both the number the model kept reaching for and a claim a PM
    could not check against any quote. It is a ranking input, not a price.
    """
    # Dead capital's exposure is an ANNUAL CARRYING COST, not money at risk of being lost. The
    # stock is not going anywhere; holding it costs this much every year. Printing it as
    # "Rs 7.12 crore at risk" invites a PM to read a recurring cost as an imminent loss.
    #
    # NOT fixed here, and worth deciding separately: the ranking still compares that recurring
    # figure against one-time exposures on a single axis, which is how a Rs 7.12 cr/yr holding
    # cost outranked a Rs 3.25 cr production block on the first live run. Naming the unit at
    # least makes the mismatch visible to the reader instead of hiding it.
    if finding.finding_type == F.DEAD_CAPITAL:
        return f"{format_scale(finding.exposure)} a year to hold"
    if finding.action_cost <= 0:
        # Nothing was subtracted, so printing the same figure twice with a parenthetical about
        # allowing for cost is just noise -- and noise in a money line is where a reader starts
        # wondering which of the two numbers to trust.
        return f"{format_scale(finding.exposure)} at risk"
    return (
        f"{format_scale(finding.decision_value)} "
        f"({format_scale(finding.exposure)} at risk, ranked after allowing for how "
        f"expensive the cheapest fix is)"
    )


def _assumptions_line(finding: F.Finding) -> str:
    if not finding.assumptions_used:
        return "none — this figure depends on no policy setting"
    return "; ".join(
        f"{key} = {entry['display']} ({entry['kind']}: {entry['basis']})"
        for key, entry in sorted(finding.assumptions_used.items())
    )



def _prior_decisions_block(finding: F.Finding) -> str:
    """What the PM decided about this subject before, verbatim.

    Its own section rather than a line in the evidence dump, because it is the one thing in the
    brief the PM wrote themselves and the model must not paraphrase it. A rejection previously
    removed a finding silently and forever; if it comes back, the reader is owed the reason they
    gave last time, in their words.
    """
    history = finding.evidence.get("prior_decisions")
    if not history:
        return ""

    lines = ["\nPREVIOUSLY DECIDED (quote the note verbatim; never paraphrase or soften it):"]
    for entry in history:
        exposure_then = entry.get("exposure_then")
        moved = ""
        if exposure_then:
            ratio = finding.exposure / float(exposure_then)
            if ratio >= 1.1 or ratio <= 0.9:
                moved = (
                    f"; worth {format_scale(float(exposure_then))} then, "
                    f"{format_scale(finding.exposure)} now ({ratio:.1f}x)"
                )
        reason = entry.get("reason")
        reason_text = f" [{REASON_LABELS.get(reason, reason)}]" if reason else ""
        lines.append(
            f"  {entry['decided_on']} {entry['decision']}{reason_text}: "
            f"\"{entry['note']}\"{moved}"
        )
    return "\n".join(lines)

def quote_lines(found: list[F.Finding]) -> tuple[list[dict], list[F.Finding]]:
    """Exact `persist_quote` arguments — one decidable line per finding, all eight types.

    `fact_restock_request` was purchase-shaped: part, warehouse, supplier, quantity. Six of the
    eight action types do not fit that (a transfer has a donor warehouse and no supplier; a
    lead-time signal has no part at all), so three quarters of what the system finds could be
    described but not approved.

    The table now carries `ACTION_TYPE`, `SUBJECT_KEY`, `SOURCE_WAREHOUSE_KEY`,
    `RECOMMENDED_SUPPLIER_KEY` and `EXPOSURE_AT_DECISION` so every action is a decidable row
    (schema_changes §1.4). `ACTION_TYPE` is the load-bearing one: the grain is no longer
    purchases only, so **summing `REQUESTED_QTY` across action types mixes units bought with
    units moved**. Any consumer of this table has to filter on it.

    `SUBJECT_KEY` matters for suppression: a supplier-grain decision cannot be matched back to
    its finding through `PART_KEY`, because there is no part.

    Still returned separately: findings with no part AND no supplier, which nothing in the table
    can address. Currently none of the eight produce that, so the list should stay empty -- it
    exists so a future scanner at a new grain fails visibly rather than silently dropping rows.
    """
    lines: list[dict] = []
    unpersistable: list[F.Finding] = []
    total = sum(f.decision_value for f in found)

    for finding in found:
        if not finding.part_id and not finding.supplier_id:
            unpersistable.append(finding)
            continue

        purchase = finding.evidence.get("purchase", {})
        quantity = _requested_qty(finding, purchase)

        lines.append(
            {
                "item_id": finding.part_id,
                "warehouse_id": finding.warehouse_id,
                "current_stock_qty": int(finding.evidence.get("on_hand_qty", 0)),
                "reorder_point_qty": int(
                    finding.evidence.get("cover_threshold_days", 0)
                    * finding.evidence.get("forward_burn", 0)
                ),
                "suggested_reorder_qty": quantity,
                "initial_urgency": _urgency(finding, total),
                # The columns that make a non-purchase action decidable.
                "action_type": finding.action_type,
                "finding_type": finding.finding_type,
                "subject_key": finding.suppression_key or finding.subject_id,
                "source_warehouse_id": finding.evidence.get("donor_warehouse_id")
                or _donor_from_subject(finding),
                "recommended_supplier_id": _recommended_supplier(finding),
                "exposure_at_decision": round(finding.exposure, 2),
            }
        )

    return lines, unpersistable


def _requested_qty(finding: F.Finding, purchase: dict) -> int:
    """The quantity this action asks for — which means different things per action type.

    Units to buy for a purchase, units to move for a transfer, units to review for dead capital.
    That ambiguity is exactly why `ACTION_TYPE` has to be read alongside it.
    """
    if finding.action_type == F.ACTION_PURCHASE:
        return int(purchase.get("orderable_qty", 0)) or int(
            finding.evidence.get("units_blocked", 0)
        )
    if finding.action_type == F.ACTION_TRANSFER:
        return int(finding.evidence.get("transfer_qty", 0))
    if finding.finding_type == F.DEAD_CAPITAL:
        return int(finding.evidence.get("on_hand_qty", 0))
    if finding.finding_type == F.DEMAND_SHIFT:
        # The recommended new safety stock, not a quantity to order.
        return int(finding.evidence.get("safety_stock_shortfall_units", 0))
    return 0


def _donor_from_subject(finding: F.Finding) -> str | None:
    """`P0012:WH001->WH002` -> `WH001`, for a transfer whose evidence omits the donor id."""
    if finding.action_type != F.ACTION_TRANSFER or "->" not in finding.subject_id:
        return None
    _, _, route = finding.subject_id.partition(":")
    donor, _, _receiver = route.partition("->")
    return donor or None


def _recommended_supplier(finding: F.Finding) -> str | None:
    if finding.finding_type != F.SUPPLIER_ECONOMICS:
        return None
    recommended = finding.evidence.get("recommended", {})
    return recommended.get("supplier_id") or None


def build_brief(found: list[F.Finding], selection_report: dict) -> Brief:
    """The complete message for the Supervisor's single turn."""
    lines, unpersistable = quote_lines(found)
    total = len(found)

    sections = []
    for index, finding in enumerate(found, start=1):
        sections.append(
            _TEMPLATE.format(
                index=index,
                total=total,
                recommendation=finding.action_detail or f"review {finding.subject_id}",
                options=_options_block(finding),
                evidence_lines=_evidence_lines(finding),
                prior_decisions=_prior_decisions_block(finding),
                if_wrong=_if_wrong(finding),
                decision_value=_decision_value_line(finding),
                assumptions=_assumptions_line(finding),
            )
        )

    header = (
        f"{selection_report.get('considered', total)} findings were assessed this run; "
        f"{total} are raised below. Every figure in each section is already computed — write "
        f"the prose around them and do not recompute, restate or infer any number. Where a "
        f"section already contains a complete arithmetic trail, reproduce it exactly.\n"
    )

    footer = ""
    if lines:
        footer = (
            "\n\nAfter writing the report, call persist_quote once with exactly these lines "
            f"(they are already resolved to PART_IDs — do not substitute names):\n{lines}\n"
            "Then call send_human_review once."
        )
    else:
        footer = (
            "\n\nNone of these findings is a restock line, so there is nothing to persist as a "
            "quote. Call send_human_review once with the report."
        )

    return Brief(
        text=header + "\n".join(sections) + footer,
        quote_lines=lines,
        unpersistable=unpersistable,
    )
