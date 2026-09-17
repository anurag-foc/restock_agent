"""The trust benchmark: eleven planted problems, and whether the system found each one.

## Why this and not the confusion matrix

`scoring.py` grades one question — will this pair run short — and that is the right question for
exactly three of the eight scanners. Measured on one world, 47 of 99 findings fell outside it
entirely, and the four items the budget actually put in front of a PM scored as worth nothing
because three of them were a transfer, a cascade and dead capital. A metric that cannot see
five-eighths of the product cannot establish trust in it.

This measures the other thing, and the thing a client actually asks: **when a problem of this
kind exists, does the system find it, and does it stay quiet when it doesn't?**

## Why it is cheap: the answers were written down first

`generation/scenarios.py` is a hand-authored catalog of eleven findings — which part, which
warehouse, which supplier, carrying which planted problem — and every other generator reads from
it rather than choosing its own numbers. It was built to gate the detector redesign, so it
already covers all eight scanners and it is labelled by construction. Nothing here invents a
truth rule; it reads the catalog and checks the output against it.

Two of the eleven are **negative** tests, and they are the half that makes the benchmark worth
anything. F9 requires silence on intermittent spares — lumpy demand must not be read as an
imminent shortage. F10 requires that most of the network stays quiet at all. A benchmark of
positives only rewards a detector that fires on everything, which is the incumbent failure this
product exists to fix.

## What a row does and does not claim

A pass says: *the planted problem of this kind was found, on the right subject.* It does not say
the system finds every problem of that kind in the world, and it does not say a finding of that
kind is always right — the catalog labels ~33 pairs, not all 278. Recall over the whole universe
needs truth at each type's own grain, which does not exist yet for five of the eight types.

So this is a capability-and-reliability benchmark, not an accuracy score, and it should be
presented as one. It is honest, it is checkable line by line, and it is the strongest evidence
available today.

Several rows check more than presence, because presence alone is a weak claim:

- F1 checks the transfer names the **best** donor, not the decoy — "find any surplus" would pass
  a presence check and is not the product.
- F2 checks the cascade names **both** co-binding children, since "buy X and unblock" is false
  when Y binds at the same level.
- F5 checks the recommendation is **not** the cheapest quote, which is the entire finding.

## Known drift in the dataset, found by this benchmark

F1's catalog nominates WH004 as the best donor and WH006 as a decoy holding more units with
thinner cover. `generation/replenishment.py` gives the two *neutral* warehouses 35 days of cover
each so they stay out of the contest — but that target is applied to the closing balance, and
WH005's **available** stock (on hand plus in transit) lands at 2112 against an intended ~1085.
A warehouse designed to be irrelevant is therefore the strongest donor in the data, and the
scenario no longer stages the contest it describes.

`generation/assertions.py` does not catch this: it checks the receiver is short and the decoy
holds more than the best donor, and never checks that a neutral holds less than either. Adding
that assertion would be correct and would fail on the current dataset, which is why it is
recorded here rather than added quietly — regenerating the dataset moves every number in this
benchmark and in `scripts/compare_baseline.py`.

The detector is not implicated. FX1 rejects WH004 for a stated and correct reason, and F1's
check below tests the property the scenario is about rather than the donor id it predicted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_restock.detectors import findings as F
from agentic_restock.detectors.scanners import MIN_P_STOCKOUT
from agentic_restock.generation import scenarios

PASS = "PASS"
FAIL = "FAIL"
CHECK = "CHECK"
"""Neither a clean pass nor the failure the test exists to catch — the system did something
defensible that the catalog did not predict. Reported separately rather than rounded to either,
because rounding it up flatters us and rounding it down invents a defect."""


@dataclass(frozen=True)
class Check:
    """One planted problem, and what the pipeline did about it."""

    finding_id: str
    name: str
    proves: str
    finding_type: str
    """The scanner this planted problem belongs to."""
    negative: bool = False
    """True for F9/F10 — a pass means the system stayed quiet."""
    result: str = FAIL
    detail: str = ""
    subject: str = ""
    selected: bool = False
    """Whether it also survived the output budget and reached the PM. Not part of pass/fail:
    the budget is a product decision about what to show, not a statement about what was found."""

    @property
    def passed(self) -> bool:
        return self.result == PASS


def _by_type(found: list[F.Finding], finding_type: str) -> list[F.Finding]:
    return [f for f in found if f.finding_type == finding_type]


def _spec(fid: str) -> dict:
    return scenarios.FINDINGS[fid]


def _check_f1(found, selected) -> Check:
    """Transfer beats buying, and the donor ranking is non-trivial."""
    s = _spec("F1")
    hits = [
        f
        for f in _by_type(found, F.REDEPLOYMENT)
        if f.part_id == s["part"] and f.warehouse_id == s["receiver"]
    ]
    if not hits:
        return Check("F1", s["name"], s["proves"], F.REDEPLOYMENT,
                     detail=f"no transfer raised for {s['part']} into {s['receiver']}")
    best = max(hits, key=lambda f: f.decision_value)
    donor = str(best.evidence.get("donor_warehouse_id") or "")
    risk_after = float(best.evidence.get("donor_risk_after") or 0.0)

    # Two properties, not one donor id. The claim F1 makes is that donor ranking is
    # non-trivial -- that the detector does not simply take the biggest pile and does not
    # relieve the receiver by breaking the donor. Both are checkable without naming a winner:
    #
    #   1. The decoy is not chosen. It holds MORE units than the catalog's intended donor and
    #      has thinner cover, so a quantity-ranked detector takes it.
    #   2. The donor is left below the level at which S1 would raise it as its own finding.
    #      "Moving a shortage is not a fix" is FX1's own stated rule, and MIN_P_STOCKOUT is
    #      the threshold the product already uses to decide a stockout is worth raising.
    #
    # Asserting `donor == best_donor` instead was measured to fail on a correct system. The
    # catalog nominates WH004 on days-of-cover reasoning, but WH004's buffer is thin in
    # absolute units: giving the receiver what it needs takes WH004's own stockout
    # probability from 0.000 to 0.494. FX1 rejected it for exactly the reason it should and
    # chose a donor that lands at 0.056. See this module's note on the generator drift that
    # made a "neutral" warehouse the strongest donor in the first place.
    if donor == s["decoy_donor"]:
        result, detail = FAIL, (
            f"chose the decoy donor {s['decoy_donor']}, which holds more units than "
            f"{s['best_donor']} but has thinner cover -- ranked by quantity held rather "
            f"than by cover given up"
        )
    elif risk_after >= MIN_P_STOCKOUT:
        result, detail = FAIL, (
            f"chose {donor}, but leaves it at a {risk_after:.0%} chance of stocking out "
            f"itself -- above the {MIN_P_STOCKOUT:.0%} bar at which that donor becomes the "
            f"next finding. The shortage was moved, not fixed"
        )
    else:
        note = "" if donor == s["best_donor"] else (
            f" (not the catalog's nominal {s['best_donor']}, which this transfer would have "
            f"pushed to a 49% stockout of its own)"
        )
        result, detail = PASS, (
            f"avoided the decoy {s['decoy_donor']} and left donor {donor} at a "
            f"{risk_after:.0%} chance of stocking out{note}"
        )
    return Check(
        "F1", s["name"], s["proves"], F.REDEPLOYMENT,
        result=result,
        subject=f"{s['part']} {donor or '?'} -> {s['receiver']}",
        detail=detail,
        selected=any(f is best for f in selected),
    )


def _check_f2(found, selected) -> Check:
    """Cascade at the parent, naming the whole binding set."""
    s = _spec("F2")
    hits = [
        f
        for f in _by_type(found, F.CASCADE_BLOCK)
        if f.part_id == s["parent_assembly"] and f.warehouse_id == s["warehouse"]
    ]
    if not hits:
        return Check("F2", s["name"], s["proves"], F.CASCADE_BLOCK,
                     detail=f"no cascade raised for {s['parent_assembly']}@{s['warehouse']}")
    cascade = hits[0]
    named = {str(c) for c in (cascade.evidence.get("binding_children") or ())}
    expected = set(s["binding_children"])
    missing = expected - named
    return Check(
        "F2", s["name"], s["proves"], F.CASCADE_BLOCK,
        result=PASS if not missing else FAIL,
        subject=f"{s['parent_assembly']}@{s['warehouse']}",
        detail=(
            f"named both binding children {sorted(expected)}"
            if not missing
            else f"named {sorted(named)}, missing {sorted(missing)} -- "
                 f"'fix one child' would be false"
        ),
        selected=any(f is cascade for f in selected),
    )


def _check_f3(found, selected) -> Check:
    """A part both short and cascade-binding priced at the production consequence."""
    s = _spec("F3")
    pair = (s["part"], s["warehouse"])
    direct = [
        f for f in found
        if (f.part_id, f.warehouse_id) == pair and f.finding_type == F.STOCKOUT_RISK
    ]
    parent = [
        f for f in _by_type(found, F.CASCADE_BLOCK)
        if f.part_id == s["parent_assembly"] and f.warehouse_id == s["warehouse"]
    ]
    hit = (direct or parent)
    if not hit:
        return Check("F3", s["name"], s["proves"], F.STOCKOUT_RISK,
                     detail=f"neither {s['part']}@{s['warehouse']} nor its parent "
                            f"{s['parent_assembly']} was raised")
    where = "at the part" if direct else f"at the parent {s['parent_assembly']}"
    basis = str(hit[0].evidence.get("consequence_basis") or "")
    return Check(
        "F3", s["name"], s["proves"], F.STOCKOUT_RISK,
        result=PASS,
        subject=f"{s['part']}@{s['warehouse']}",
        detail=f"raised {where}" + (f", consequence basis {basis.lower()}" if basis else ""),
        selected=any(f in selected for f in hit),
    )


def _check_f4(found, selected) -> Check:
    """Lead time measures spread as well as drift — both suppliers must be reported."""
    s = _spec("F4")
    reported = {f.supplier_id for f in _by_type(found, F.LEADTIME_SIGNAL)}
    want = {s["erratic_supplier"], s["drifting_supplier"]}
    missing = want - reported
    return Check(
        "F4", s["name"], s["proves"], F.LEADTIME_SIGNAL,
        result=PASS if not missing else FAIL,
        subject=", ".join(sorted(want)),
        detail=(
            f"both reported: {s['erratic_supplier']} (spread), "
            f"{s['drifting_supplier']} (drift)"
            if not missing
            else f"missing {sorted(missing)} -- a mean-only test sees nothing wrong with "
                 f"a supplier on contract with a wide spread"
        ),
        selected=any(f.supplier_id in want for f in selected),
    )


def _check_f5(found, selected) -> Check:
    """The cheapest quote is not the cheapest supplier."""
    s = _spec("F5")
    cheapest = s["suppliers"][0][0]
    hits = [f for f in _by_type(found, F.SUPPLIER_ECONOMICS) if f.part_id == s["part"]]
    if not hits:
        return Check("F5", s["name"], s["proves"], F.SUPPLIER_ECONOMICS,
                     detail=f"no supplier comparison raised for {s['part']}")
    # `supplier_id` is the INCUMBENT on this finding type; the recommendation is in evidence.
    # Reading the wrong one scored a correct reversal as a failure the first time this ran.
    finding = hits[0]
    chosen = str((finding.evidence.get("recommended") or {}).get("supplier_id") or "")
    ok = bool(chosen) and chosen != cheapest
    return Check(
        "F5", s["name"], s["proves"], F.SUPPLIER_ECONOMICS,
        result=PASS if ok else FAIL,
        subject=s["part"],
        detail=(
            f"recommended {chosen} over the cheapest quote {cheapest} "
            f"(reversed = {finding.evidence.get('quoted_price_ranking_reversed')})"
            if ok
            else f"recommended the cheapest quote {cheapest} -- the quoted-price ranking "
                 f"was not reversed"
        ),
        selected=any(f in selected for f in hits),
    )


def _check_f6(found, selected) -> Check:
    """MOQ forces an overbuy costing more than the risk it removes."""
    s = _spec("F6")
    hits = [f for f in _by_type(found, F.MOQ_UNECONOMIC) if f.part_id == s["part"]]
    return Check(
        "F6", s["name"], s["proves"], F.MOQ_UNECONOMIC,
        result=PASS if hits else FAIL,
        subject=f"{s['part']} / {s['supplier']}",
        detail=(
            f"raised as its own action type, not a footnote on a buy "
            f"(MOQ {s['moq']} against {s['target_daily_burn']}/day)"
            if hits
            else f"no MOQ finding for {s['part']}"
        ),
        selected=any(f in selected for f in hits),
    )


def _check_f7(found, selected) -> Check:
    """Demand shifted and the safety stock did not move with it."""
    s = _spec("F7")
    hits = [
        f for f in _by_type(found, F.DEMAND_SHIFT)
        if f.part_id == s["part"] and f.warehouse_id == s["warehouse"]
    ]
    return Check(
        "F7", s["name"], s["proves"], F.DEMAND_SHIFT,
        result=PASS if hits else FAIL,
        subject=f"{s['part']}@{s['warehouse']}",
        detail=(
            f"caught the {s['step_factor']}x step from {s['step_days_ago']} days ago"
            if hits
            else (
                f"missed the {s['step_factor']}x step planted {s['step_days_ago']} days ago"
                + (
                    f" -- the pair was raised, but as "
                    f"{sorted({f.finding_type for f in found if (f.part_id, f.warehouse_id) == (s['part'], s['warehouse'])})}, "
                    f"so the nuance was not surfaced as its own finding"
                    if any(
                        (f.part_id, f.warehouse_id) == (s["part"], s["warehouse"])
                        for f in found
                    )
                    else ""
                )
            )
        ),
        selected=any(f in selected for f in hits),
    )


def _check_f8(found, selected) -> Check:
    """Dead capital — the excess half of the network view."""
    s = _spec("F8")
    pairs = {tuple(p) for p in s["pairs"]}
    raised = {(f.part_id, f.warehouse_id) for f in _by_type(found, F.DEAD_CAPITAL)}
    hit = pairs & raised
    return Check(
        "F8", s["name"], s["proves"], F.DEAD_CAPITAL,
        result=PASS if hit else FAIL,
        subject=f"{len(pairs)} planted pairs",
        detail=(
            f"found {len(hit)} of {len(pairs)} "
            f"({s['cover_days']} days cover, quiet {s['quiet_days']} days)"
            if hit
            else f"found none of the {len(pairs)} planted dead-stock pairs"
        ),
        selected=any((f.part_id, f.warehouse_id) in pairs for f in selected),
    )


def _check_f9(found, selected) -> Check:
    """NEGATIVE — lumpy demand must not be read as an imminent shortage."""
    s = _spec("F9")
    parts, warehouse = set(s["parts"]), s["warehouse"]
    silent = set(s["silent_scanners"])
    noisy = [
        f for f in found
        if f.finding_type in silent and f.part_id in parts and f.warehouse_id == warehouse
    ]
    return Check(
        "F9", s["name"], s["proves"], "/".join(sorted(silent)), negative=True,
        result=PASS if not noisy else FAIL,
        subject=f"{len(parts)} intermittent spares @ {warehouse}",
        detail=(
            f"stayed quiet on all {len(parts)} "
            f"(demand every ~{s['mean_interval_days']} days)"
            if not noisy
            else f"cried shortage on {len(noisy)}: "
                 f"{sorted({f.part_id for f in noisy})}"
        ),
    )


def _check_f10(found, selected, pair_count: int) -> Check:
    """NEGATIVE — quiet must be the normal case. The alert-fatigue thesis, as a test."""
    s = _spec("F10")
    flagged = {(f.part_id, f.warehouse_id) for f in found if f.part_id and f.warehouse_id}
    quiet = max(pair_count - len(flagged), 0)
    fraction = quiet / pair_count if pair_count else 0.0
    ok = fraction >= s["min_quiet_fraction"]
    return Check(
        "F10", s["name"], s["proves"], "all scanners", negative=True,
        result=PASS if ok else FAIL,
        subject=f"{pair_count} pairs",
        detail=(
            f"{fraction:.0%} of the network raised nothing "
            f"(floor {s['min_quiet_fraction']:.0%})"
        ),
    )


def _check_f11(found, selected) -> Check:
    """Does decision value order differently from raw exposure?"""
    s = _spec("F11")
    high = [
        f for f in found
        if f.part_id == s["high_exposure_part"]
        and f.warehouse_id == s["high_exposure_warehouse"]
    ]
    transfer = [
        f for f in found
        if f.part_id == s["transfer_fixable_part"]
        and f.warehouse_id == s["transfer_fixable_warehouse"]
    ]
    if not high or not transfer:
        missing = []
        if not high:
            missing.append(f"{s['high_exposure_part']}@{s['high_exposure_warehouse']}")
        if not transfer:
            missing.append(
                f"{s['transfer_fixable_part']}@{s['transfer_fixable_warehouse']}"
            )
        return Check("F11", s["name"], s["proves"], "ranking",
                     detail=f"not both raised; missing {missing}")
    # By decision value, not exposure. The claim under test is that pricing the fix reorders
    # the list, so each pair must be represented by the finding the ranking would actually
    # pick. Taking the largest-exposure finding instead read P0070's stockout line (fix cost
    # >= exposure, decision value 0) and missed the transfer that produces the inversion.
    h = max(high, key=lambda f: f.decision_value)
    t = max(transfer, key=lambda f: f.decision_value)
    inverted = (h.exposure > t.exposure) and (t.decision_value > h.decision_value)
    return Check(
        "F11", s["name"], s["proves"], "ranking",
        result=PASS if inverted else FAIL,
        subject=f"{s['high_exposure_part']} vs {s['transfer_fixable_part']}",
        detail=(
            f"bigger exposure ranks lower once the fix is priced: "
            f"{s['high_exposure_part']} at risk {h.exposure:,.0f} but worth "
            f"{h.decision_value:,.0f} to act on; {s['transfer_fixable_part']} at risk "
            f"{t.exposure:,.0f} but worth {t.decision_value:,.0f} ({t.finding_type})"
            if inverted
            else f"no inversion: exposure order and decision-value order agree "
                 f"({s['high_exposure_part']} {h.exposure:,.0f}/{h.decision_value:,.0f} vs "
                 f"{s['transfer_fixable_part']} {t.exposure:,.0f}/{t.decision_value:,.0f})"
        ),
        selected=any(f in selected for f in (h, t)),
    )


@dataclass(frozen=True)
class Benchmark:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def needs_check(self) -> int:
        return sum(1 for c in self.checks if c.result == CHECK)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.result == FAIL)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def positives(self) -> list[Check]:
        return [c for c in self.checks if not c.negative]

    @property
    def negatives(self) -> list[Check]:
        return [c for c in self.checks if c.negative]

    @property
    def types_covered(self) -> set[str]:
        """Distinct finding types a *passing* check exercises. The coverage headline.

        Restricted to real finding types: F10 covers "all scanners" and F11 covers the ranking
        rather than a detector, and counting either as a ninth kind of problem would overstate
        the coverage this benchmark actually demonstrates.
        """
        return {
            c.finding_type
            for c in self.checks
            if c.passed and not c.negative and c.finding_type in F.ALL_FINDING_TYPES
        }

    @property
    def types_total(self) -> int:
        return len(F.ALL_FINDING_TYPES)


def run(detected: list[F.Finding], selected: list[F.Finding], pair_count: int) -> Benchmark:
    """Grade one pipeline run against the eleven planted findings."""
    return Benchmark(
        checks=[
            _check_f1(detected, selected),
            _check_f2(detected, selected),
            _check_f3(detected, selected),
            _check_f4(detected, selected),
            _check_f5(detected, selected),
            _check_f6(detected, selected),
            _check_f7(detected, selected),
            _check_f8(detected, selected),
            _check_f9(detected, selected),
            _check_f10(detected, selected, pair_count),
            _check_f11(detected, selected),
        ]
    )
