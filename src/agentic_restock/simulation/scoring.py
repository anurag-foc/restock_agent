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
        }


def _pair_of(finding: F.Finding) -> tuple[str, str] | None:
    if finding.part_id and finding.warehouse_id:
        return (finding.part_id, finding.warehouse_id)
    return None


def _by_pair(found: list[F.Finding]) -> tuple[dict[tuple[str, str], list[F.Finding]], int]:
    out: dict[tuple[str, str], list[F.Finding]] = {}
    unscored = 0
    for finding in found:
        pair = _pair_of(finding)
        if pair is None:
            unscored += 1
            continue
        out.setdefault(pair, []).append(finding)
    return out, unscored


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
    detected_by_pair, unscored = _by_pair(detected)
    selected_by_pair, _ = _by_pair(selected)

    outcomes: list[PairOutcome] = []
    for row in part_position.itertuples(index=False):
        pair = (row.PART_ID, row.WAREHOUSE_ID)
        fact = truth.get(pair)
        if fact is None:
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

    return Scorecard(pairs=outcomes, unscored_findings=unscored)
