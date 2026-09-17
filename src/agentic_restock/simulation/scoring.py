"""The confusion matrix, priced in rupees.

The measurable performance indicator the simulation exists to produce
(docs/simulation_feature_design.md §2). Forecast accuracy is the wrong axis: MASE is not money,
and the translation from "the burn estimate was 12% high" to "that would have cost ₹4.2 lakh"
runs entirely through this system's own pricing. So every cell is filled with a figure the
pipeline already computed — `exposure` from `estimators/risk.py`, `action_cost` from
`detectors/fixes.py` — and nothing new is invented to price an outcome.

                          | pair will really run short | pair is really fine
    pipeline raised it    | + decision_value           | - action_cost
    pipeline stayed quiet | - exposure                 | 0

Three of those deserve their justification stated, because they are what will be argued about:

- **A catch is credited `decision_value`, not `exposure`.** `exposure - action_cost` is what the
  finding is worth net of acting on it. Crediting full exposure would score an expensive fix as
  highly as a cheap one — the error the redesign already corrected once in the ranking.
- **A miss costs full `exposure`, and should dominate.** A tool that is quiet and wrong is worse
  than one that is noisy and right, and the metric has to say so.
- **A false alarm costs `action_cost`, not exposure.** A PM acting on a finding that was not real
  spends the fix cost; they do not lose an exposure that never existed. This is the only cell
  that punishes a loose detector, and it is the alert-fatigue claim with a number on it.

**Truth decides the cell; the product's own pricing decides the amount.** Whether a pair really
needed action comes from `truth.py` — the generator's parameters, not the pipeline's opinion of
itself. What that outcome is worth comes from the pipeline. Letting the pipeline supply both
would make the score unfalsifiable.

**Scored twice, at detection and at selection.** The output budget means a pair can be found
correctly and still never reach the PM. Scoring only what the PM saw would blame the detectors
for the budget; scoring only what was detected would hide the budget's cost entirely. Reporting
both makes `budget_cost` a number — which is the sharpest evidence for the product's
"a handful of actions, twice a day" thesis, and the sharpest against it if it comes out badly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from agentic_restock.detectors import findings as F
from agentic_restock.simulation.truth import PairTruth

CELL_CAUGHT = "CAUGHT"
CELL_MISSED = "MISSED"
CELL_FALSE_ALARM = "FALSE_ALARM"
CELL_CORRECTLY_QUIET = "CORRECTLY_QUIET"


@dataclass(frozen=True)
class PairOutcome:
    """One (part, warehouse) scored against what was really going to happen."""

    part_id: str
    warehouse_id: str
    regime: str
    will_run_short: bool
    shortfall_qty: float

    detected: bool
    """A scanner produced a finding, whether or not it survived the budget."""
    selected: bool
    """It reached the PM."""

    cell: str
    """Scored on `selected` — what the PM actually saw. The product KPI."""
    value: float
    detect_cell: str
    """Scored on `detected` — what the detectors were capable of, budget ignored."""
    detect_value: float

    exposure: float
    action_cost: float
    planted: tuple[str, ...]
    finding_types: tuple[str, ...]


def _cell_and_value(raised: bool, short: bool, exposure: float, cost: float) -> tuple[str, float]:
    if short and raised:
        return CELL_CAUGHT, exposure - cost
    if short:
        return CELL_MISSED, -exposure
    if raised:
        return CELL_FALSE_ALARM, -cost
    return CELL_CORRECTLY_QUIET, 0.0


@dataclass(frozen=True)
class Scorecard:
    pairs: list[PairOutcome] = field(default_factory=list)
    unscored_findings: int = 0
    """Findings at a grain with no pair — supplier-level S5 and S7 — which pair-keyed truth
    cannot judge. Reported rather than dropped silently: a run where this is large is a run
    whose net value describes less of the pipeline than it appears to."""
    non_shortage_findings: int = 0
    """Findings whose type does not address a shortage (S4, S6, S8 and the rest), dropped
    because realised-shortage truth cannot judge them either way. This is the largest single
    slice of the pipeline the score says nothing about — a run reporting a handful of items of
    which most land here has been measured on very little of what it actually did."""
    out_of_scope_pairs: int = 0
    """Pairs excluded by `PairTruth.in_scope` — parts the factory builds rather than buys. The
    denominator every count above is NOT taken over."""

    def _cell(self, name: str, *, detection: bool = False) -> list[PairOutcome]:
        attr = "detect_cell" if detection else "cell"
        return [p for p in self.pairs if getattr(p, attr) == name]

    @property
    def caught(self) -> list[PairOutcome]:
        return self._cell(CELL_CAUGHT)

    @property
    def missed(self) -> list[PairOutcome]:
        return self._cell(CELL_MISSED)

    @property
    def false_alarms(self) -> list[PairOutcome]:
        return self._cell(CELL_FALSE_ALARM)

    @property
    def correctly_quiet(self) -> list[PairOutcome]:
        return self._cell(CELL_CORRECTLY_QUIET)

    @property
    def value_delivered(self) -> float:
        return sum(p.value for p in self.caught)

    @property
    def value_missed(self) -> float:
        return -sum(p.value for p in self.missed)

    @property
    def value_wasted(self) -> float:
        return -sum(p.value for p in self.false_alarms)

    @property
    def net_value(self) -> float:
        """What the PM's four items were worth, net of everything they did not see."""
        return sum(p.value for p in self.pairs)

    @property
    def detector_net_value(self) -> float:
        """The same, if every finding reached the PM. The ceiling the budget trades against."""
        return sum(p.detect_value for p in self.pairs)

    @property
    def budget_cost(self) -> float:
        """What the output budget costs, in rupees. Never negative by construction — the
        budget can only remove findings, not add them."""
        return self.detector_net_value - self.net_value

    @property
    def recall(self) -> float:
        expected = len(self.caught) + len(self.missed)
        return len(self.caught) / expected if expected else 0.0

    @property
    def precision(self) -> float:
        raised = len(self.caught) + len(self.false_alarms)
        return len(self.caught) / raised if raised else 0.0

    @property
    def detector_caught(self) -> list[PairOutcome]:
        """Spotted by a scanner, whether or not the budget let it through.

        A count, not a ratio, because "spotted 32 of the 76 that were going to go wrong" is a
        sentence anyone can check and "detector recall 0.42" is not.
        """
        return self._cell(CELL_CAUGHT, detection=True)

    @property
    def detector_false_alarms(self) -> list[PairOutcome]:
        return self._cell(CELL_FALSE_ALARM, detection=True)

    @property
    def detector_recall(self) -> float:
        caught = len(self._cell(CELL_CAUGHT, detection=True))
        missed = len(self._cell(CELL_MISSED, detection=True))
        return caught / (caught + missed) if caught + missed else 0.0

    @property
    def detector_precision(self) -> float:
        caught = len(self._cell(CELL_CAUGHT, detection=True))
        false = len(self._cell(CELL_FALSE_ALARM, detection=True))
        return caught / (caught + false) if caught + false else 0.0

    def by_regime(self, *, detection: bool = False) -> dict[str, dict]:
        """The scorecard split by demand regime.

        A single net value hides the thing internal validation most needs to know: whether an
        estimator wins everywhere or only on the smooth parts. Croston exists for intermittent
        demand, so an engine that beats the incumbent on `smooth` and loses on `erratic` has
        not earned the claim its name makes -- and the aggregate cannot say which happened.
        """
        attr = "detect_cell" if detection else "cell"
        value_attr = "detect_value" if detection else "value"
        out: dict[str, dict] = {}
        for pair in self.pairs:
            bucket = out.setdefault(
                pair.regime,
                {
                    "pairs": 0,
                    "at_risk": 0,
                    CELL_CAUGHT: 0,
                    CELL_MISSED: 0,
                    CELL_FALSE_ALARM: 0,
                    CELL_CORRECTLY_QUIET: 0,
                    "net_value": 0.0,
                },
            )
            bucket["pairs"] += 1
            bucket["at_risk"] += int(pair.will_run_short)
            bucket[getattr(pair, attr)] += 1
            bucket["net_value"] += getattr(pair, value_attr)
        return out

    def false_alarms_by_type(self, *, detection: bool = False) -> dict[str, int]:
        """Which scanner raised each false alarm.

        Load-bearing caveat, not a nicety: truth here is *realised shortage*, so a
        `DEAD_CAPITAL` or `REDEPLOYMENT` finding on a pair that is not going to run short is
        scored a false alarm -- when in fact it is a different, real problem the yardstick
        cannot see. Reading the false-alarm count without this breakdown overstates the noise
        of any scanner that is not S1.
        """
        cell = CELL_FALSE_ALARM
        attr = "detect_cell" if detection else "cell"
        out: dict[str, int] = {}
        for pair in self.pairs:
            if getattr(pair, attr) != cell:
                continue
            for finding_type in pair.finding_types or ("(none)",):
                out[finding_type] = out.get(finding_type, 0) + 1
        return out

    def to_row(self) -> dict:
        return {
            "PAIRS_TOTAL": len(self.pairs),
            "PAIRS_AT_RISK": sum(1 for p in self.pairs if p.will_run_short),
            "CAUGHT": len(self.caught),
            "MISSED": len(self.missed),
            "FALSE_ALARMS": len(self.false_alarms),
            "CORRECTLY_QUIET": len(self.correctly_quiet),
            "DETECTOR_CAUGHT": len(self.detector_caught),
            "DETECTOR_FALSE_ALARMS": len(self.detector_false_alarms),
            "VALUE_DELIVERED": round(self.value_delivered, 2),
            "VALUE_MISSED": round(self.value_missed, 2),
            "VALUE_WASTED": round(self.value_wasted, 2),
            "NET_VALUE": round(self.net_value, 2),
            "DETECTOR_NET_VALUE": round(self.detector_net_value, 2),
            "BUDGET_COST": round(self.budget_cost, 2),
            "RECALL": round(self.recall, 4),
            "PRECISION": round(self.precision, 4),
            "DETECTOR_RECALL": round(self.detector_recall, 4),
            "DETECTOR_PRECISION": round(self.detector_precision, 4),
            "UNSCORED_FINDINGS": self.unscored_findings,
            "NON_SHORTAGE_FINDINGS": self.non_shortage_findings,
            "OUT_OF_SCOPE_PAIRS": self.out_of_scope_pairs,
        }


SHORTAGE_ADDRESSING = (F.STOCKOUT_RISK, F.CASCADE_BLOCK, F.REDEPLOYMENT)
"""The finding types that, if acted on, stop a pair running short.

Truth here is realised shortage, so only a finding that *addresses* a shortage may be scored
against it. Before this restriction a `DEAD_CAPITAL` finding on a pair that happened to be
short was credited with catching the shortage — dead capital is the opposite problem, and the
budget's four picks in one measured run contained a dead-capital finding scored as a catch.
The same error ran the other way: a dead-capital finding on a healthy pair was scored a false
alarm, when it is a different real problem the yardstick cannot see. 23 of 35 "false alarms"
in that run were this.

`REDEPLOYMENT` is included because S3 keys its finding to the **receiving** warehouse
(`warehouse_id=best.receiver_warehouse_id`), so the pair it is scored against is the one the
transfer actually relieves.

`DEMAND_SHIFT` is deliberately **excluded**, and it is the arguable one. Acting on it
recalibrates a safety stock that would eventually trigger a buy, so it does prevent a shortage
at one remove — but it places no order itself. Understating our own score is the safe direction
for any figure that will be shown to a client, which is the same reason `baseline.py` gives the
incumbent the stronger of its two rules. `non_shortage_findings` counts what this drops so the
choice is visible rather than silent.
"""


def covered_pairs(finding: F.Finding) -> list[tuple[str, str]]:
    """Every (part, warehouse) whose shortage this one finding would relieve.

    Normally just its own subject. A `CASCADE_BLOCK` is the exception and the reason this
    function exists: it is raised at the *parent* and names the whole binding set, and
    `selection.py` collapses a child's own S1 finding into it as a duplicate. Scoring the
    subject alone therefore charged the pipeline with missing exactly the children it had just
    correctly folded into the parent — one measured run selected a cascade on P0002@WH001 at
    Rs 10.91 cr while its binding child P0042@WH001, at Rs 10.91 cr, was scored a miss.

    This is open question 2 in docs/simulation_feature_design.md §8. Note it credits *coverage*,
    not value: the parent's exposure is still counted once, at the parent, exactly as
    `parent_cascade` counts it — crediting each child its own exposure would reintroduce the
    double-count the redesign removed.
    """
    if not finding.warehouse_id:
        return []
    out: list[tuple[str, str]] = []
    if finding.part_id:
        out.append((finding.part_id, finding.warehouse_id))
    if finding.finding_type == F.CASCADE_BLOCK:
        for child in finding.evidence.get("binding_children") or ():
            if child:
                out.append((str(child), finding.warehouse_id))
    return out


def _by_pair(found: list[F.Finding]) -> tuple[dict[tuple[str, str], list[F.Finding]], int, int]:
    """Index findings by every pair they cover, dropping the ones truth cannot judge."""
    out: dict[tuple[str, str], list[F.Finding]] = {}
    unscored = 0
    non_shortage = 0
    for finding in found:
        if finding.finding_type not in SHORTAGE_ADDRESSING:
            non_shortage += 1
            continue
        pairs = covered_pairs(finding)
        if not pairs:
            unscored += 1
            continue
        for pair in pairs:
            out.setdefault(pair, []).append(finding)
    return out, unscored, non_shortage


def type_precision(
    findings: list[F.Finding], finding_type: str, truth: dict[tuple[str, str], PairTruth]
) -> tuple[int, int]:
    """Of the findings of one shortage-addressing type, how many point at a pair truth confirms
    was really going to run short, and how many could be checked at all.

    Deliberately **not** the same computation `score()` uses for the product KPI. `score()`
    collapses every finding touching a pair into one outcome, credited to whichever finding had
    the highest decision value — right for "what did the PM's list deliver," wrong for "of this
    *type's* claims, how many were right," which is what a benchmark chart showing one bar per
    type needs. A REDEPLOYMENT finding and a STOCKOUT_RISK finding on the same pair are each
    graded on their own claim here, not folded into one winner.

    Restricted to `SHORTAGE_ADDRESSING` on purpose. `covered_pairs()` returns a pair for a
    `DEAD_CAPITAL` or `DEMAND_SHIFT` finding too (both carry `part_id`/`warehouse_id`), and
    checking those against *shortage* truth would silently score a different claim than the one
    the finding actually makes -- a dead-capital finding on a pair that happens not to be short
    is not wrong, it was never predicting a shortage in the first place.

    Returns `(correct, checked)`. `checked` can be well below the count of findings raised --
    `REDEPLOYMENT` reaches in-house parts truth has no purchase-lead-time answer for, and those
    are excluded rather than guessed at. Reporting both numbers, not just a percentage, is what
    keeps a small, honest denominator from reading as a suspiciously perfect one.
    """
    if finding_type not in SHORTAGE_ADDRESSING:
        return (0, 0)
    checked = 0
    correct = 0
    for finding in findings:
        if finding.finding_type != finding_type:
            continue
        pairs = [p for p in covered_pairs(finding) if p in truth and truth[p].in_scope]
        if not pairs:
            continue
        checked += 1
        if any(truth[p].will_run_short for p in pairs):
            correct += 1
    return (correct, checked)


def score(
    part_position: pd.DataFrame,
    truth: dict[tuple[str, str], PairTruth],
    detected: list[F.Finding],
    selected: list[F.Finding],
) -> Scorecard:
    """Score one run against the generated world's realised outcomes.

    `part_position` supplies the exposure of pairs that produced no finding at all — without it
    a miss could not be priced, since there is no finding to read a number off. That asymmetry
    is what the whole metric turns on: the pipeline's silence still has a cost, and it is only
    visible from outside.
    """
    detected_by_pair, unscored, non_shortage = _by_pair(detected)
    selected_by_pair, _, _ = _by_pair(selected)

    outcomes: list[PairOutcome] = []
    out_of_scope = 0
    for row in part_position.itertuples(index=False):
        pair = (row.PART_ID, row.WAREHOUSE_ID)
        fact = truth.get(pair)
        if fact is None:
            continue
        if not fact.in_scope:
            out_of_scope += 1
            continue

        here = detected_by_pair.get(pair, [])
        chosen = selected_by_pair.get(pair, [])

        # The best finding on the pair decides the money. A pair raised for two reasons is
        # still one thing on the PM's list, and crediting both would let a noisy scanner
        # inflate the score by restating the same problem twice.
        best = max(chosen or here, key=lambda f: f.decision_value, default=None)
        fallback_exposure = float(getattr(row, "EXPOSURE", 0.0) or 0.0)
        exposure = float(best.exposure) if best else fallback_exposure
        action_cost = float(best.action_cost) if best else 0.0

        cell, value = _cell_and_value(bool(chosen), fact.will_run_short, exposure, action_cost)
        detect_cell, detect_value = _cell_and_value(
            bool(here), fact.will_run_short, exposure, action_cost
        )

        outcomes.append(
            PairOutcome(
                part_id=pair[0],
                warehouse_id=pair[1],
                regime=fact.regime,
                will_run_short=fact.will_run_short,
                shortfall_qty=fact.shortfall_qty,
                detected=bool(here),
                selected=bool(chosen),
                cell=cell,
                value=float(value),
                detect_cell=detect_cell,
                detect_value=float(detect_value),
                exposure=exposure,
                action_cost=action_cost,
                planted=fact.planted,
                finding_types=tuple(sorted({f.finding_type for f in (chosen or here)})),
            )
        )

    return Scorecard(
        pairs=outcomes,
        unscored_findings=unscored,
        non_shortage_findings=non_shortage,
        out_of_scope_pairs=out_of_scope,
    )
