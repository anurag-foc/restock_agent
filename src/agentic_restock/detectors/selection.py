"""Suppression and selection — turning 183 findings into the three or four a PM sees.

Two jobs, and the second matters more than it looks.

**Suppression (nuance 8)** drops a finding while a decision on it is still live. This lives in
code rather than in an instruction to a model, because an LLM that remembers to filter 97% of the
time re-raises a rejected item roughly monthly — and a PM who sees something they already
rejected stops trusting the whole queue. A commitment suppresses only while it is *fresh*: once
it has sat longer than a defensible turnaround its exposure is still accruing, so it comes back
flagged as stalled.

Note what `STALLED_COMMITMENT` is here: a **flag on a finding**, not a finding type. In the
superseded design it was one of three `signal_type` values, which made the live signal set look
broader than it was — it was a relabel of the other two, so the system claimed three categories
while having two. Modelling it as an attribute is the fix.

**Selection** enforces the hard output budget. Scarcity is the feature: one real MRP run produced
8,366 action messages in a week, and a system that emits everything true is the incumbent problem
with better prose.

Selection diversifies across finding type deliberately, and Phase 4 measured why that matters
more than the ranking formula does. Ranking by decision value rather than raw exposure reorders
the middle of the list substantially (Spearman 0.577, 240 of 286 findings moving 10+ places) but
leaves the **top ten identical** — at the top, exposure exceeds any fix cost by two to three
orders of magnitude, so subtracting the cost cannot reorder anything. With a budget of three or
four, the ranking formula therefore changes nothing a PM ever sees. What does change it is
refusing to spend the whole budget on one kind of problem.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from agentic_restock.detectors import findings as F

# How long a decision may sit before its finding comes back. A PM turnaround of two days is
# defensible; beyond that the exposure is still accruing while nothing happens.
PENDING_STALE_DAYS = 2

# An approved order gets its own lead time plus a grace buffer before it counts as stalled.
APPROVED_GRACE_DAYS = 3

# How much bigger a rejected finding must get before it is worth asking again. A rejection is
# an answer, not a snooze, so nothing re-raises on a timer -- only on the situation changing
# enough that it is a different question.
REJECTION_RESURFACE_MULTIPLE = 1.5

# Structured reason codes. The PM picks one alongside the free-text note, and it is the reason
# code -- never the prose -- that changes behaviour.
#
# The distinction that forced this to exist: "this is not a real problem" and "I cannot act on
# this right now" are opposite signals which read almost identically in free text. Treating the
# second as the first is how a system learns to stop surfacing hard-but-important findings,
# which is alert fatigue inverted and much harder to notice than the original. So a snooze
# comes back, and a no does not.
REASON_NOT_A_PROBLEM = "NOT_A_PROBLEM"
REASON_ALREADY_HANDLED = "ALREADY_HANDLED"
REASON_CANNOT_ACT_NOW = "CANNOT_ACT_NOW"
REASON_NUMBERS_WRONG = "NUMBERS_WRONG"
REASON_OTHER = "OTHER"

REASON_CODES = (
    REASON_NOT_A_PROBLEM,
    REASON_ALREADY_HANDLED,
    REASON_CANNOT_ACT_NOW,
    REASON_NUMBERS_WRONG,
    REASON_OTHER,
)

# How long a "cannot act right now" rejection stays quiet before the finding returns. Long
# enough not to nag inside one working week; short enough that a real constraint which has since
# lifted does not stay buried for a quarter.
SNOOZE_DAYS = 14

# A reason that judges the FINDING (it was wrong, or the numbers were) closes it until the
# situation materially changes. A reason that judges the MOMENT does not.
REASONS_THAT_CLOSE = frozenset({REASON_NOT_A_PROBLEM, REASON_NUMBERS_WRONG})

# How many past decisions on the same subject to carry into the brief. Enough to show a pattern
# ("rejected twice for the same reason"), few enough that the report stays a page.
MAX_PRIOR_DECISIONS = 3

# Statuses that suppress while fresh, and how their age is measured.
PENDING_STATUSES = ("PENDING_APPROVAL", "NEEDS_REVIEW")
IN_EXECUTION_STATUSES = ("APPROVED", "FULFILLING")

# A closed decision. Never re-raised: the answer was no.
REJECTED_STATUS = "REJECTED"

# Default output budget. Deliberately small -- see the module docstring.
DEFAULT_BUDGET = 4

# At most this many findings of any one type, so a single noisy scanner cannot take the budget.
MAX_PER_TYPE = 2


@dataclass(frozen=True)
class Commitment:
    """An existing decision against a subject, as recorded in `fact_restock_request`."""

    suppression_key: str
    status: str
    requested_on: date
    decided_on: date | None = None
    lead_days: float = 30.0
    # What the PM wrote when they decided, and what the finding was worth at the time. Both are
    # new: NOTE was always there but never read back, and EXPOSURE_AT_DECISION did not exist
    # until the schema was extended. Together they turn a decision from a gate into a record.
    note: str | None = None
    exposure_at_decision: float | None = None
    # One of REASON_CODES. None on every line decided before the field existed, which must
    # behave exactly as before -- see `suppresses`.
    reason: str | None = None

    def age_days(self, as_of: date) -> int:
        anchor = self.decided_on if self.status in IN_EXECUTION_STATUSES else self.requested_on
        anchor = anchor or self.requested_on
        return (as_of - anchor).days

    def is_stale(self, as_of: date) -> bool:
        """Whether this decision has sat long enough that its finding should come back."""
        if self.status == REJECTED_STATUS:
            return False  # a closed decision stays closed
        age = self.age_days(as_of)
        if self.status in PENDING_STATUSES:
            return age > PENDING_STALE_DAYS
        if self.status in IN_EXECUTION_STATUSES:
            return age > self.lead_days + APPROVED_GRACE_DAYS
        return False

    def suppresses(self, as_of: date, current_exposure: float | None = None) -> bool:
        """Whether this decision should keep its finding off the queue right now.

        Only a *live* decision suppresses. An earlier version returned `not is_stale(...)`, which
        made every terminal status suppress forever, because a terminal status is never stale --
        so a part whose last order COMPLETED sixty days ago could never be raised again. After a
        few months of operation that would have muted most of the catalog silently.
        """
        if self.status == REJECTED_STATUS:
            # "Cannot act right now" is a snooze, not an answer. It comes back on its own,
            # because the constraint that blocked it -- a budget freeze, a shutdown, a person on
            # leave -- lifts without anyone telling this system. Suppressing it permanently
            # would quietly delete a real problem for the most ordinary of reasons.
            if self.reason == REASON_CANNOT_ACT_NOW:
                return self.age_days(as_of) <= SNOOZE_DAYS

            # "No" holds until the situation materially changes. Time alone is not a good enough
            # reason to re-ask -- that is how a queue becomes noise a PM learns to dismiss --
            # but a finding now worth 1.5x what it was worth when it was declined is a
            # different question, not the same one repeated. This was a documented gap for as
            # long as the table had no EXPOSURE_AT_DECISION column to compare against.
            if (
                current_exposure is not None
                and self.exposure_at_decision
                and current_exposure >= self.exposure_at_decision * REJECTION_RESURFACE_MULTIPLE
            ):
                return False
            return True
        if self.status in PENDING_STATUSES or self.status in IN_EXECUTION_STATUSES:
            return not self.is_stale(as_of)
        return False  # COMPLETED, or anything unrecognised: not a live decision



def _with_history(finding: F.Finding, commitments: list[Commitment]) -> F.Finding:
    """Attach what the PM decided about this subject before, and what they said.

    A rejection used to remove a finding silently and permanently, so the reasoning went into
    the table and never came out. That wasted the single most informative thing in the system:
    a domain expert explaining, in their own words, why the machine was wrong. Showing it back
    costs one join and changes the report from a fresh assertion into a continuing conversation
    -- and it lets the model write "you declined this in August because you had dual-sourced it;
    exposure has since doubled" instead of raising the same item as though for the first time.

    Deliberately NOT a learned weight. This is recall, not inference: the note is reproduced,
    never interpreted, and it does not touch the ranking. Turning free text into a score needs a
    structured reason code -- "not a real problem" and "can't act right now" are opposite
    signals that read almost identically in prose.
    """
    decided = [c for c in commitments if c.decided_on is not None]
    if not decided:
        return finding

    history = [
        {
            "decided_on": c.decided_on.isoformat(),
            "decision": c.status,
            "note": (c.note or "").strip() or "(no note given)",
            "reason": c.reason,
            "exposure_then": c.exposure_at_decision,
        }
        for c in sorted(decided, key=lambda c: c.decided_on, reverse=True)[:MAX_PRIOR_DECISIONS]
    ]
    return replace(finding, evidence={**finding.evidence, "prior_decisions": history})

def apply_suppression(
    found: list[F.Finding], commitments: list[Commitment], *, as_of: date
) -> list[F.Finding]:
    """Drop findings with a live decision against them; flag the ones whose decision has stalled.

    Keyed on `suppression_key`, which each scanner sets at *its own* grain. That matters: a
    supplier-grain finding is suppressed by a decision about that supplier, not by a decision
    about one of its twelve parts. The superseded design suppressed inside one function's WHERE
    clause over (part, warehouse) rows, so a supplier-level signal had no way to be suppressed
    at all.
    """
    by_key: dict[str, list[Commitment]] = {}
    for commitment in commitments:
        by_key.setdefault(commitment.suppression_key, []).append(commitment)

    kept: list[F.Finding] = []
    for finding in found:
        relevant = by_key.get(finding.suppression_key, [])
        if not relevant:
            kept.append(finding)
            continue

        if any(c.suppresses(as_of, finding.exposure) for c in relevant):
            continue

        stalled = max(
            (c for c in relevant if c.is_stale(as_of)),
            key=lambda c: c.age_days(as_of),
            default=None,
        )
        if stalled is None:
            kept.append(_with_history(finding, relevant))
            continue

        # Re-surfaced. The evidence says so explicitly, because "this was already raised and
        # nothing happened for 14 days" is a materially different message from a fresh finding
        # and a PM reading the second as the first will wonder why they are seeing it again.
        # History goes on this branch too. A stalled item is precisely the one where the PM's
        # own earlier note explains what it is waiting on.
        with_history = _with_history(finding, relevant)
        kept.append(
            replace(
                with_history,
                evidence={
                    **with_history.evidence,
                    "stalled_commitment": {
                        "status": stalled.status,
                        "age_days": stalled.age_days(as_of),
                        "requested_on": stalled.requested_on.isoformat(),
                        "decided_on": (
                            stalled.decided_on.isoformat() if stalled.decided_on else None
                        ),
                    },
                },
            )
        )
    return kept


def collapse_duplicates(found: list[F.Finding]) -> list[F.Finding]:
    """Merge findings that describe the same underlying problem.

    Diversifying across finding *type* is not enough on its own, because two types can describe
    one root cause. Two collapses, both observed spending real budget slots:

    **A cascade subsumes its binding children's shortages.** `CASCADE_BLOCK P0002: needs P0042`
    and `STOCKOUT_RISK P0042: buy 4601 units` are the same action seen from two ends. They took
    two of four slots on one problem. The cascade wins: it carries the production consequence and
    names the whole binding set, where the child finding sees only its own shortfall.

    **A transfer and a purchase on the same pair are alternative fixes, not two problems.** The
    cheaper one wins and the other is recorded as the option considered — which is also what the
    report needs in order to show a rejected alternative rather than asserting one existed.
    """
    cascade_children: dict[tuple[str, str], F.Finding] = {}
    for finding in found:
        if finding.finding_type != F.CASCADE_BLOCK:
            continue
        for child in finding.evidence.get("binding_children", []):
            cascade_children[(child, finding.warehouse_id)] = finding

    # Alternative fixes for one (part, warehouse): keep the best, remember the other.
    by_pair: dict[tuple[str, str], list[F.Finding]] = {}
    passthrough: list[F.Finding] = []
    for finding in found:
        if (
            finding.finding_type in (F.STOCKOUT_RISK, F.REDEPLOYMENT)
            and finding.part_id
            and finding.warehouse_id
        ):
            by_pair.setdefault((finding.part_id, finding.warehouse_id), []).append(finding)
        else:
            passthrough.append(finding)

    out: list[F.Finding] = list(passthrough)
    for pair, options in by_pair.items():
        if pair in cascade_children:
            continue  # the cascade already covers this, and covers it better

        best = max(options, key=lambda f: f.decision_value)
        alternatives = [o for o in options if o is not best]
        if alternatives:
            best = replace(
                best,
                evidence={
                    **best.evidence,
                    "alternative_options": [
                        {
                            "action_type": alt.action_type,
                            "action_detail": alt.action_detail,
                            "action_cost": round(alt.action_cost, 2),
                            "decision_value": round(alt.decision_value, 2),
                        }
                        for alt in alternatives
                    ],
                },
            )
        out.append(best)

    return out


def select(
    found: list[F.Finding],
    *,
    budget: int = DEFAULT_BUDGET,
    max_per_type: int = MAX_PER_TYPE,
) -> list[F.Finding]:
    """The three or four findings a PM sees, diverse by type and ranked by decision value.

    Round-robin across types rather than taking a global top-N. A global top-N spends the whole
    budget on whichever scanner happens to produce the largest numbers -- and since exposure
    varies by orders of magnitude between types (a blocked A-CRITICAL assembly against an
    unattractive minimum order), that is guaranteed to be the same scanner every run. The PM
    would then never see the transfer opportunity, which the evidence base calls the strongest
    action available.
    """
    if budget <= 0 or not found:
        return []

    ranked = sorted(collapse_duplicates(found), key=lambda f: f.decision_value, reverse=True)

    by_type: dict[str, list[F.Finding]] = {}
    for finding in ranked:
        by_type.setdefault(finding.finding_type, []).append(finding)

    # Types in order of their single best finding, so the strongest category leads.
    type_order = sorted(
        by_type, key=lambda t: by_type[t][0].decision_value, reverse=True
    )

    selected: list[F.Finding] = []
    taken_per_type: dict[str, int] = {}
    round_index = 0

    while len(selected) < budget and round_index < max_per_type:
        progressed = False
        for finding_type in type_order:
            if len(selected) >= budget:
                break
            candidates = by_type[finding_type]
            index = taken_per_type.get(finding_type, 0)
            if index >= len(candidates) or index > round_index:
                continue
            selected.append(candidates[index])
            taken_per_type[finding_type] = index + 1
            progressed = True
        if not progressed:
            break
        round_index += 1

    return sorted(selected, key=lambda f: f.decision_value, reverse=True)


def selection_report(found: list[F.Finding], selected: list[F.Finding]) -> dict:
    """What was considered and what got through — the evidence for the quiet-run claim.

    The alert-fatigue argument needs this on the record: "we looked at 183 things and raised 4"
    is only a claim if nobody wrote the 183 down.
    """
    from collections import Counter

    return {
        "considered": len(found),
        "selected": len(selected),
        "considered_by_type": dict(Counter(f.finding_type for f in found)),
        "selected_by_type": dict(Counter(f.finding_type for f in selected)),
        "total_decision_value_selected": sum(f.decision_value for f in selected),
        "total_decision_value_available": sum(f.decision_value for f in found),
        "stalled_resurfaced": sum(
            1 for f in selected if "stalled_commitment" in f.evidence
        ),
    }
