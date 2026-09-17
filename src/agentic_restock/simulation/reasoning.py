"""Plain-English reasoning for one example finding of each type.

The benchmark chart shows a count per finding type — how many of each kind of problem the
system found. A count on its own is a claim nobody can check. This module turns one real
finding of each type back into the sentence a person who has never heard of `p_stockout` or
`coefficient_of_variation` would actually want: the real part or supplier, and the numbers that
specific kind of problem reasons from.

**The numbers are not the same shape for every type, on purpose.** "Running out" reasons from
stock on hand, daily usage and lead time. "Move stock instead of buying" reasons from two
warehouses' days of cover. "Cheaper supplier" reasons from a quoted price against a reject rate.
Forcing all eight through one template would either drop the numbers that actually convinced the
system, or pad the ones that didn't. So each type gets its own sentence, built from the fields
that scanner actually populated on `finding.evidence` — nothing here is invented; every number
already existed on the finding before this module ran.

`explain()` is the only entry point. It never raises on a finding shape it does not recognise —
a scanner's evidence dict changing shape should degrade to a generic sentence, not break a page
that exists to build trust.
"""

from __future__ import annotations

from agentic_restock.detectors import findings as F
from agentic_restock.money import format_inr as _inr
from agentic_restock.simulation.scoring import SHORTAGE_ADDRESSING, type_precision

# Plain-language name for each finding type, as a client would say it — never the internal
# constant. Shared with the page so the chart, the reasoning and any future surface agree on
# what to call each one.
PLAIN_NAME: dict[str, str] = {
    F.STOCKOUT_RISK: "Running out",
    F.CASCADE_BLOCK: "Assembly line blocked",
    F.REDEPLOYMENT: "Move stock instead of buying",
    F.DEAD_CAPITAL: "Stock that will never move",
    F.LEADTIME_SIGNAL: "Supplier slipping",
    F.DEMAND_SHIFT: "Demand changed, buffer didn't",
    F.SUPPLIER_ECONOMICS: "Cheaper supplier available",
    F.MOQ_UNECONOMIC: "Bad order size",
}


def _round(value: object, digits: int = 0) -> str:
    """Format a number, or raise.

    Deliberately does not swallow a missing field into `str(None)` -- an evidence dict of a
    shape this explainer was not written for (the ERP arm's own `STOCKOUT_RISK` findings carry
    `naive_daily_consumption`/`contracted_lead_days`, not `forward_burn`/`mu_lead_days`) must
    fail loudly enough for `explain()` to fall back to the finding's own `action_detail`, not
    silently print "None a day" into a sentence a client reads. That silent-fill shape is the
    exact failure `docs/redesign_tracker.md` records twice.
    """
    if value is None:
        raise ValueError("missing field")
    return f"{float(value):,.{digits}f}"


def _erp_stockout_risk(f: F.Finding) -> str:
    """The ERP's own rule, in words, grounded in one real instance.

    `STOCKOUT_RISK` is shared with `_stockout_risk` below -- both a real S1 finding and the
    incumbent's own breach carry this finding type, because that is honestly what the ERP rule
    is: a cruder stockout trigger. They are told apart by `"rule" in evidence`, which only
    `baseline.py`'s arms set. Getting this dispatch wrong is exactly the bug this file's tests
    exist to catch -- an earlier version fell through to the S1 explainer here and silently
    printed "None a day" for every ERP example.
    """
    e = f.evidence
    where = f"{f.part_id} at {f.warehouse_id}"
    available = e.get("available_qty")
    safety = e.get("safety_stock_qty")

    if e.get("rule") == "erp_safety_stock":
        return (
            f"{where} — stock fell to {_round(available)} units, at or below the safety "
            f"stock of {_round(safety)}. That is the whole test: it does not ask how fast "
            f"the part is moving or how long a new order takes to arrive."
        )

    naive = e.get("naive_daily_consumption")
    lead = e.get("contracted_lead_days")
    point = e.get("reorder_point")
    return (
        f"{where} — stock fell to {_round(available)} units. That's at or below the "
        f"reorder point of {_round(point)}: the safety stock of {_round(safety)}, plus what "
        f"it expects to use over the {_round(lead)}-day delivery wait at "
        f"{_round(naive, 1)} a day. It reacts only once that line is crossed — it never "
        f"asks whether {_round(naive, 1)} a day is still the right number to plan against."
    )


def _stockout_risk(f: F.Finding) -> str:
    e = f.evidence
    on_hand = e.get("on_hand_qty", e.get("available_qty"))
    burn = e.get("forward_burn")
    cover = e.get("days_of_cover")
    lead = e.get("mu_lead_days")
    return (
        f"{f.part_id} at {f.warehouse_id} — {_round(on_hand)} units on hand, using about "
        f"{_round(burn, 1)} a day. That's {_round(cover, 0)} days of stock. The supplier "
        f"takes about {_round(lead, 0)} days to deliver — it runs out before the next "
        f"order can land."
    )


def _cascade_block(f: F.Finding) -> str:
    e = f.evidence
    children = e.get("binding_children") or []
    blocked = e.get("units_blocked")
    if len(children) > 1:
        parts = " and ".join(children)
        return (
            f"{f.part_id} at {f.warehouse_id} — needs {parts} to keep building, and both "
            f"are short at the same time. {_round(blocked)} units can't be assembled. "
            f"Buying just one of them wouldn't restart the line."
        )
    part = children[0] if children else "a component"
    return (
        f"{f.part_id} at {f.warehouse_id} — held up by {part}. "
        f"{_round(blocked)} units can't be assembled until it arrives."
    )


def _redeployment(f: F.Finding) -> str:
    e = f.evidence
    donor = e.get("donor_warehouse_id", "another warehouse")
    receiver = e.get("receiver_warehouse_id", f.warehouse_id)
    donor_before = e.get("donor_cover_after_days")
    qty = e.get("transfer_qty")
    donor_risk_after = e.get("donor_risk_after")
    return (
        f"{f.part_id} — {receiver} is running short. {donor} has enough to spare: sending "
        f"{_round(qty)} units still leaves {donor} with about {_round(donor_before, 0)} "
        f"days of stock afterwards"
        + (
            f" (about a {float(donor_risk_after) * 100:.0f}% chance {donor} itself runs "
            f"short — checked before recommending it)"
            if donor_risk_after is not None
            else ""
        )
        + "."
    )


def _dead_capital(f: F.Finding) -> str:
    e = f.evidence
    on_hand = e.get("on_hand_qty")
    cover = e.get("days_of_cover")
    stopped = e.get("burn_method") == "STOPPED" or cover is None
    where = f"{f.part_id} at {f.warehouse_id}"
    if stopped:
        return f"{where} — {_round(on_hand)} units on hand. Nothing has moved off the shelf in a long time."
    return (
        f"{where} — {_round(on_hand)} units on hand, {_round(cover, 0)} days of stock at "
        f"the current rate of use. That is far more than it would ever need before it stops "
        f"being used at all."
    )


def _leadtime_signal(f: F.Finding) -> str:
    e = f.evidence
    contracted = e.get("contracted_lead_days")
    mu = e.get("observed_mu_lead_days")
    sigma = e.get("observed_sigma_lead_days")
    parts = e.get("affected_parts") or []
    n_parts = len(parts)
    return (
        f"{f.subject_id} — promises {_round(contracted, 0)} days. Real deliveries average "
        f"{_round(mu, 0)} days but swing by about ±{_round(sigma, 0)} days: sometimes weeks "
        f"early, sometimes weeks late. Affects {n_parts} part{'s' if n_parts != 1 else ''}."
    )


def _demand_shift(f: F.Finding) -> str:
    e = f.evidence
    prior = e.get("prior_daily_rate")
    recent = e.get("recent_daily_rate")
    return (
        f"{f.part_id} at {f.warehouse_id} — using about {_round(recent, 1)} a day now, "
        f"against {_round(prior, 1)} a day six months ago. The safety stock was never "
        f"raised to match."
    )


def _supplier_economics(f: F.Finding) -> str:
    e = f.evidence
    cur = e.get("current") or {}
    rec = e.get("recommended") or {}
    saving = e.get("annual_saving")
    return (
        f"{f.part_id} — {cur.get('supplier_id', 'the current supplier')} quotes "
        f"{_inr(float(cur.get('quoted_unit_cost', 0)))}/unit but rejects "
        f"{float(cur.get('reject_rate', 0)) * 100:.0f}% of deliveries. "
        f"{rec.get('supplier_id', 'the alternative')} quotes "
        f"{_inr(float(rec.get('quoted_unit_cost', 0)))}/unit with "
        f"{float(rec.get('reject_rate', 0)) * 100:.0f}% rejects — cheaper overall by about "
        f"{_inr(float(saving or 0))} a year."
    )


def _moq_uneconomic(f: F.Finding) -> str:
    e = f.evidence
    moq = e.get("moq")
    required = e.get("required_qty")
    months = e.get("excess_months")
    return (
        f"{f.part_id} / {f.supplier_id} — the minimum order is {_round(moq)} units, but "
        f"only {_round(required)} are actually needed right now. That buys about "
        f"{_round(months, 1)} months more stock than the problem it's meant to fix."
    )


_EXPLAINERS = {
    F.STOCKOUT_RISK: _stockout_risk,
    F.CASCADE_BLOCK: _cascade_block,
    F.REDEPLOYMENT: _redeployment,
    F.DEAD_CAPITAL: _dead_capital,
    F.LEADTIME_SIGNAL: _leadtime_signal,
    F.DEMAND_SHIFT: _demand_shift,
    F.SUPPLIER_ECONOMICS: _supplier_economics,
    F.MOQ_UNECONOMIC: _moq_uneconomic,
}


def explain(finding: F.Finding) -> str:
    """One plain-English sentence, built from the fields that finding's own scanner populated.

    Falls back to the finding's own `action_detail` (already plain-language, if terser) rather
    than raising, so a scanner's evidence shape changing does not break the page — it just
    produces a less specific sentence until this module is updated to match.
    """
    if finding.finding_type == F.STOCKOUT_RISK and "rule" in finding.evidence:
        fn = _erp_stockout_risk
    else:
        fn = _EXPLAINERS.get(finding.finding_type)
    if fn is None:
        return finding.action_detail or finding.exposure_basis
    try:
        return fn(finding)
    except (KeyError, TypeError, ValueError, IndexError):
        return finding.action_detail or finding.exposure_basis


def best_example(findings: list[F.Finding], finding_type: str) -> F.Finding | None:
    """The example shown for a type: highest decision value, so the chart's example is the
    strongest instance of that kind of problem rather than an arbitrary first match."""
    candidates = [f for f in findings if f.finding_type == finding_type]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.decision_value)


def _accuracy_note(finding_type: str, findings: list, truth: dict | None) -> str:
    """Plain words for 'of the ones it raised, how many turned out to be real' -- or nothing,
    for a type there is no true answer to check against yet.

    Deliberately reports both numbers, not a percentage. `checked` is often well below the
    count found -- a REDEPLOYMENT target that is an in-house part has no purchase-lead-time
    answer for truth to check against -- and a bare "100%" on a small, silently-filtered
    denominator is exactly the kind of figure that reads as too good to be true. Saying "we
    could check 11 of the 33 -- all 11 were right" is slower to read and survives being
    questioned, which is the only kind of number worth putting in front of a client on this page.
    """
    if truth is None or finding_type not in SHORTAGE_ADDRESSING:
        return ""
    correct, checked = type_precision(findings, finding_type, truth)
    if checked == 0:
        return ""
    total = sum(1 for f in findings if f.finding_type == finding_type)
    if checked < total:
        return (
            f"We could check {checked} of the {total} it flagged against what really "
            f"happened on the test warehouse — {correct} of those {checked} were right."
        )
    return (
        f"Checked against what really happened on the test warehouse — "
        f"{correct} of {checked} were right."
    )


def type_summary_rows(findings: list, truth: dict | None = None) -> list[dict]:
    """Count + one worked example per finding type, ready for `persistence.build_sim_type_summary_insert`.

    Only types this arm actually produced get a row -- an incumbent rule that only ever emits
    `STOCKOUT_RISK` should show one row, not eight rows of which seven are zero. A zero-count row
    for a type the arm cannot express at all would understate how structurally narrow it is;
    omitting it is the honest way to say "this arm has no concept of this problem."

    `truth`, when given, adds a plain-language accuracy note for the three finding types that
    predict a shortage (`scoring.SHORTAGE_ADDRESSING`) -- see `_accuracy_note`. The other five
    types get an empty note: there is no ground truth yet to check them against, and printing a
    number there would be inventing one.
    """
    from collections import Counter

    counts = Counter(f.finding_type for f in findings)
    rows = []
    for finding_type, count in counts.items():
        example = best_example(findings, finding_type)
        rows.append(
            {
                "finding_type": finding_type,
                "count": count,
                "example_subject": example.subject_id if example else "",
                "example_reasoning": explain(example) if example else "",
                "example_exposure": float(example.exposure) if example else 0.0,
                "accuracy_note": _accuracy_note(finding_type, findings, truth),
            }
        )
    return rows
