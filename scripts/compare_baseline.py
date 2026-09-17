"""Score the incumbent ERP rule against our engines on one generated world, to the terminal.

**Internal validation, not a demo surface.** It writes nothing -- no `sim_run` row, no page, no
catalog read. The point is to find out what the numbers actually are before anything is designed
around them, because designing the chart first is how you end up tuning the measurement to feed
it. `docs/redesign_tracker.md` records that failure shape twice.

    PYTHONPATH=src python3 scripts/compare_baseline.py
    PYTHONPATH=src python3 scripts/compare_baseline.py --budget 4 --no-statsforecast

Read the output expecting the incumbent to *win* on recall. A rule that flags every breach
catches nearly everything -- that is what emitting thousands of messages a week buys you. If it
does, the honest comparison is on alert volume, precision and money, not on how many were
spotted. Deciding that after seeing the numbers is the whole reason this script exists.

Every figure is simulated against a demand model we wrote. See
`docs/simulation_feature_design.md` §2.2: it is not measured savings and never becomes measured
savings by being charted.
"""

from __future__ import annotations

import argparse
import sys

from agentic_restock import settings as st
from agentic_restock.money import format_inr as _inr
from agentic_restock.simulation import baseline, run
from agentic_restock.simulation.scoring import (
    CELL_CAUGHT,
    CELL_FALSE_ALARM,
    CELL_MISSED,
    Scorecard,
)

# Two scans a day, five days a week -- the cadence the Lakeflow job runs at (07:00 + 15:00 UTC).
# Used only to put alert volume in the unit a PM feels it in.
SCANS_PER_WEEK = 10

RULE = "─"


def _hr(width: int = 96) -> str:
    return RULE * width


def _fmt_rupees(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{_inr(abs(value))}"


class Arm:
    """One row of the comparison."""

    def __init__(self, label: str, engine_run, *, budgeted: bool, note: str = ""):
        self.label = label
        self.run = engine_run
        self.budgeted = budgeted
        self.note = note

    @property
    def card(self) -> Scorecard:
        return self.run.scorecard

    @property
    def detection(self) -> bool:
        """Read the detector-side cells for an arm with no budget; there is no difference."""
        return not self.budgeted

    @property
    def alerts(self) -> int:
        return len(self.run.selected)


def build_arms(args) -> tuple[list[Arm], baseline.ErpScan, dict]:
    settings = st.DEFAULTS
    if args.budget is not None:
        values = dict(settings.values)
        values["items_per_notification"] = args.budget
        settings = st.Settings(values=values)

    print("Building the generated world (~10s, deterministic)...", flush=True)
    frames = run.world()

    # One measurement, shared. The ERP arm and the `automatic` arm are scored against
    # byte-identical positions; see baseline.py's table of what is held constant.
    cfg = run._settings_for("automatic", settings, args.budget)
    measures = run.measure(cfg, frames)
    truth = baseline.truth_for(measures, frames)

    arms: list[Arm] = []
    scans: dict[str, baseline.ErpScan] = {}

    for rule in baseline.RULES:
        print(f"Scoring {rule}...", flush=True)
        engine_run, scan = baseline.run_erp(rule, measures, truth, settings=settings)
        scans[rule] = scan
        arms.append(Arm(rule, engine_run, budgeted=False, note="no budget, no exposure floor"))

    engines = ["automatic"] if args.no_statsforecast else ["automatic", "statsforecast"]
    for engine in engines:
        print(f"Scoring ours/{engine}...", flush=True)
        engine_run = run.run_engine(
            engine, settings=settings, budget=args.budget, world_frames=frames
        )
        arms.append(
            Arm(
                f"ours/{engine} (budget {engine_run.budget})",
                engine_run,
                budgeted=True,
            )
        )
        # The same engine with the budget lifted. The gap between this row and the one above
        # it is BUDGET_COST -- what the product thesis costs, which has to be visible or the
        # thesis is being asserted rather than measured.
        arms.append(
            Arm(
                f"ours/{engine} (no budget)",
                engine_run,
                budgeted=False,
                note="detector ceiling",
            )
        )

    return arms, scans, truth


def print_header(arms: list[Arm], truth: dict, args) -> None:
    card = arms[0].card
    print()
    print(_hr())
    print(" BASELINE COMPARISON  ·  simulated on one generated world")
    print(_hr())
    print(
        f" world      {card.to_row()['PAIRS_TOTAL']} (part, warehouse) pairs  ·  "
        f"{card.to_row()['PAIRS_AT_RISK']} will really run short"
    )
    print(
        f" policy     holding {st.DEFAULTS.holding_rate:.0%}  ·  "
        f"budget {args.budget or st.DEFAULTS.items_per_notification}  ·  "
        f"min exposure {_inr(st.DEFAULTS.min_exposure)} (ours only)"
    )
    print(
        " NOTE       SIMULATED figures, not measured savings "
        "(docs/simulation_feature_design.md §2.2)"
    )
    print()


def print_table(arms: list[Arm]) -> None:
    print(_hr())
    print(
        f" {'ARM':<34}{'ALERTS':>8}{'/wk':>7}{'SPOTTED':>9}{'MISSED':>8}"
        f"{'FALSE':>7}{'NET (simulated)':>19}"
    )
    print(_hr())
    for arm in arms:
        d = arm.detection
        card = arm.card
        caught = len(card._cell(CELL_CAUGHT, detection=d))
        missed = len(card._cell(CELL_MISSED, detection=d))
        false = len(card._cell(CELL_FALSE_ALARM, detection=d))
        at_risk = caught + missed
        net = card.detector_net_value if d else card.net_value
        alerts = len(arm.run.detected) if d else arm.alerts
        print(
            f" {arm.label:<34}{alerts:>8}{alerts * SCANS_PER_WEEK:>7}"
            f"{f'{caught}/{at_risk}':>9}{missed:>8}{false:>7}{_fmt_rupees(net):>19}"
        )
    print(_hr())
    print(
        " SPOTTED counts pairs that were really going to run short.  ALERTS is items in one\n"
        " scan; /wk extrapolates at 10 scans a week.  NET = delivered - missed - wasted."
    )
    print()


def print_precision(arms: list[Arm]) -> None:
    print(" PRECISION — of what it put in front of you, how much was real")
    print(_hr())
    for arm in arms:
        d = arm.detection
        card = arm.card
        caught = len(card._cell(CELL_CAUGHT, detection=d))
        false = len(card._cell(CELL_FALSE_ALARM, detection=d))
        raised = caught + false
        share = f"{caught / raised:.0%}" if raised else "n/a"
        print(f" {arm.label:<34}{caught:>5} real of {raised:<5} raised   {share:>6}")
    print()


def print_money(arms: list[Arm]) -> None:
    print(" MONEY — broken out, because a single net figure hides how it was earned")
    print(_hr())
    print(
        f" {'ARM':<34}{'DELIVERED':>16}{'MISSED':>16}{'WASTED':>16}{'NET':>16}"
    )
    for arm in arms:
        card = arm.card
        if arm.detection:
            delivered = sum(
                p.detect_value for p in card._cell(CELL_CAUGHT, detection=True)
            )
            missed = -sum(p.detect_value for p in card._cell(CELL_MISSED, detection=True))
            wasted = -sum(
                p.detect_value for p in card._cell(CELL_FALSE_ALARM, detection=True)
            )
            net = card.detector_net_value
        else:
            delivered, missed = card.value_delivered, card.value_missed
            wasted, net = card.value_wasted, card.net_value
        print(
            f" {arm.label:<34}{_fmt_rupees(delivered):>16}{_fmt_rupees(missed):>16}"
            f"{_fmt_rupees(wasted):>16}{_fmt_rupees(net):>16}"
        )
    print()


def print_regimes(arms: list[Arm]) -> None:
    print(" PER REGIME — real shortages spotted. Does an engine win everywhere, or only on the")
    print(" smooth parts? The aggregate cannot say, and this is what Croston is for.")
    print(_hr())
    regimes = sorted({r for arm in arms for r in arm.card.by_regime()})
    header = f" {'REGIME':<14}{'AT RISK':>9}"
    for arm in arms:
        header += f"{arm.label.split(' (')[0][-16:]:>18}"
    print(header)
    for regime in regimes:
        first = arms[0].card.by_regime(detection=arms[0].detection).get(regime, {})
        line = f" {regime:<14}{first.get('at_risk', 0):>9}"
        for arm in arms:
            bucket = arm.card.by_regime(detection=arm.detection).get(regime, {})
            line += f"{bucket.get(CELL_CAUGHT, 0):>18}"
        print(line)
    print()


def print_caveats(arms: list[Arm], scans: dict) -> None:
    print(" WHAT THIS SCORE DOES NOT COVER")
    print(_hr())
    for rule, scan in scans.items():
        print(
            f" {rule}: {scan.breaches_total} pairs breached the rule, "
            f"{scan.breaches_unpriceable} dropped for having no supplier contract "
            f"(S1's universe). The incumbent's real alert volume is higher than shown."
        )
    first = arms[0].card
    print(
        f" scored universe: {len(first.pairs)} pairs. "
        f"{first.out_of_scope_pairs} in-house pairs excluded -- a purchase lead time is "
        f"meaningless for a part the factory builds, so 'will it run out before a "
        f"replenishment lands' asks about a replenishment that does not exist."
    )
    for arm in arms:
        bits = []
        if arm.card.unscored_findings:
            bits.append(f"{arm.card.unscored_findings} supplier-grain (S5/S7)")
        if arm.card.non_shortage_findings:
            bits.append(f"{arm.card.non_shortage_findings} not shortage-addressing (S4/S6/S8)")
        if bits:
            print(f" {arm.label}: {', '.join(bits)} -- not judged either way.")
    print()
    print(" FALSE ALARMS BY SCANNER — truth here is *realised shortage*, so a DEAD_CAPITAL or")
    print(" REDEPLOYMENT finding on a pair that will not run short scores as a false alarm when")
    print(" it is a different, real problem. Read the FALSE column with this in hand.")
    print(_hr())
    for arm in arms:
        by_type = arm.card.false_alarms_by_type(detection=arm.detection)
        if not by_type:
            continue
        parts = "  ".join(
            f"{k}={v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
        )
        print(f" {arm.label:<34}{parts}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="output budget for our arms; default from settings",
    )
    parser.add_argument(
        "--no-statsforecast",
        action="store_true",
        help="skip the Nixtla arm (avoids the optional dependency)",
    )
    args = parser.parse_args(argv)

    arms, scans, truth = build_arms(args)

    print_header(arms, truth, args)
    print_table(arms)
    print_precision(arms)
    print_money(arms)
    print_regimes(arms)
    print_caveats(arms, scans)

    print(_hr())
    print(" Simulated on one generated world under one policy. Not measured savings.")
    print(_hr())
    return 0


if __name__ == "__main__":
    sys.exit(main())
