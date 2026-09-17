"""One simulation run: generate, estimate, scan, select, score — all in memory.

The whole pipeline below the Spark edges is pure functions over pandas/numpy, which is what
makes this possible at all: a run touches no catalog, writes no fact table, and cannot affect
what the live pipeline sees. Only the result is persisted.

**The generated world is built once and shared across engines.** The generator is deterministic
and does not depend on the consumption model, so re-drawing it per engine would cost ~10s each
and, worse, invite a future change that made the two engines face different data. Comparing
estimators is only a valid inference if everything else is held identical — same pairs, same
demand, same suppliers, same day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import pandas as pd

from agentic_restock import settings as st
from agentic_restock.detectors import findings as F
from agentic_restock.detectors import scanners, selection
from agentic_restock.generation import dates
from agentic_restock.jobs import positions
from agentic_restock.simulation import frames as sim_frames
from agentic_restock.simulation import truth as sim_truth
from agentic_restock.simulation.scoring import Scorecard, score


@dataclass(frozen=True)
class EngineRun:
    """What one engine scored on one generated world."""

    engine: str
    budget: int
    scorecard: Scorecard
    detected: list[F.Finding]
    selected: list[F.Finding]
    truth: dict = field(default_factory=dict)
    """Keyed by (part_id, warehouse_id). Carried alongside the scorecard so a caller that wants
    per-type accuracy (`scoring.type_precision`) does not have to rebuild it -- the world and
    the policy are already fixed by the time `run_engine` returns, so a second build would only
    risk drifting from the one the score was actually computed against."""

    def to_row(self) -> dict:
        return {
            "CONSUMPTION_MODEL": self.engine,
            "BUDGET": self.budget,
            "FINDINGS_DETECTED": len(self.detected),
            "FINDINGS_SELECTED": len(self.selected),
            **self.scorecard.to_row(),
        }


@lru_cache(maxsize=1)
def world() -> dict[str, pd.DataFrame]:
    """The generated world, built once per process.

    ~10s and deterministic, so caching it is the difference between a comparison run taking
    twenty seconds and taking two minutes. Treat the frames as read-only.
    """
    return sim_frames.build()


def _settings_for(engine: str, base: st.Settings | None, budget: int | None) -> st.Settings:
    values = dict((base or st.DEFAULTS).values)
    values["consumption_model"] = engine
    if budget is not None:
        values["items_per_notification"] = budget
    return st.Settings(values=values)


@dataclass(frozen=True)
class Measures:
    """The corrected-measures layer for one world under one policy.

    Split out of `run_engine` so an arm that is *not* one of our engines -- the incumbent ERP
    rule in `baseline.py` -- can be scored against exactly the same measured position rather
    than a second one built by copied code. Two arms compared on two independently constructed
    position tables is a comparison of the construction, not of the arms.
    """

    part_position: pd.DataFrame
    cascades: pd.DataFrame
    supplier_performance: pd.DataFrame


def measure(cfg: st.Settings, f: dict[str, pd.DataFrame]) -> Measures:
    """Build supplier performance, part position and cascades, and attach exposure."""
    supplier_performance = positions.build_supplier_performance(
        f["delivery"], f["contracts"], as_of=dates.TODAY
    )
    part_position = positions.build_part_position(
        f["position"], f["issues"], supplier_performance, as_of=dates.TODAY, settings=cfg
    )
    cascades = positions.build_parent_cascades(
        part_position, f["plan"], f["model_bom"], f["bom"]
    )
    part_position = positions.attach_exposure(part_position, cascades, f["bom"])
    return Measures(
        part_position=part_position,
        cascades=cascades,
        supplier_performance=supplier_performance,
    )


def run_engine(
    engine: str,
    *,
    settings: st.Settings | None = None,
    budget: int | None = None,
    world_frames: dict[str, pd.DataFrame] | None = None,
) -> EngineRun:
    """Estimate, scan, select and score one engine against the generated world."""
    f = world_frames if world_frames is not None else world()
    cfg = _settings_for(engine, settings, budget)
    effective_budget = int(cfg.items_per_notification)

    m = measure(cfg, f)
    part_position = m.part_position

    detected = scanners.scan_all(
        part_position, m.cascades, m.supplier_performance, settings=cfg
    )

    # No suppression. `apply_suppression` reads live decisions out of fact_restock_request,
    # which belong to the real pipeline's history -- letting them reach a simulation would
    # make the score depend on what a PM happened to approve last week, and two runs of the
    # same seed would stop agreeing.
    selected = selection.select(detected, budget=effective_budget)

    truth = sim_truth.build(f["position"], sim_truth.purchasable_parts(m.supplier_performance))
    return EngineRun(
        engine=engine,
        budget=effective_budget,
        scorecard=score(part_position, truth, detected, selected),
        detected=detected,
        selected=selected,
        truth=truth,
    )


def compare(
    engines: list[str],
    *,
    settings: st.Settings | None = None,
    budget: int | None = None,
) -> list[EngineRun]:
    """Every engine against the same world, in the order given.

    The first engine is the baseline the others are read against, so pass the default first.
    """
    f = world()
    return [
        run_engine(engine, settings=settings, budget=budget, world_frames=f)
        for engine in engines
    ]
