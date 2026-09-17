"""The trust benchmark, to the terminal: eleven planted problems, graded.

    PYTHONPATH=src python3 scripts/benchmark.py

Answers the question a client actually asks — *when a problem of this kind exists, does it find
it, and does it stay quiet when it doesn't?* — rather than the one `scripts/compare_baseline.py`
answers, which is how it compares to an ERP reorder rule on shortages alone.

Reads nothing from a catalog and writes nothing. The world is generated in memory, the eleven
answers were written down in `generation/scenarios.py` before any of this existed, and every row
names its own subject so it can be checked by hand. See `simulation/benchmark.py` for what a row
does and does not claim — in particular, a pass is evidence the detector works on a planted
case, not a recall figure over the whole network.
"""

from __future__ import annotations

import sys

from agentic_restock.simulation import benchmark, run

WIDTH = 104


def main() -> int:
    print("Building the generated world (~10s, deterministic)...", flush=True)
    frames = run.world()
    result = run.run_engine("automatic", world_frames=frames)
    b = benchmark.run(result.detected, result.selected, pair_count=len(frames["position"]))

    print()
    print("═" * WIDTH)
    print(" TRUST BENCHMARK — eleven problems planted in the data, before the system saw it")
    print("═" * WIDTH)
    print()

    for check in b.checks:
        mark = {benchmark.PASS: "PASS ", benchmark.FAIL: "FAIL ", benchmark.CHECK: "CHECK"}[
            check.result
        ]
        kind = "must stay quiet" if check.negative else check.finding_type
        shown = " (shown to the PM)" if check.selected else ""
        print(f" {mark}  {check.finding_id:<4} {check.name}")
        print(f"        {kind}  ·  {check.subject}{shown}")
        print(f"        {check.detail}")
        print()

    print("─" * WIDTH)
    print(
        f" {b.passed} passed  ·  {b.failed} failed  ·  {b.needs_check} to look at"
        f"    (of {b.total})"
    )
    print(
        f" kinds of problem proven on a planted case: "
        f"{len(b.types_covered)} of {b.types_total}"
    )
    print(
        f" silence tests passed: "
        f"{sum(1 for c in b.negatives if c.passed)} of {len(b.negatives)}"
        f"    — a benchmark of positives only rewards a detector that fires on everything"
    )
    print("─" * WIDTH)
    print(
        " A pass means the planted problem of that kind was found on the right subject. It is\n"
        " not a recall figure over the whole network: the catalog labels ~33 pairs, not all 278."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
