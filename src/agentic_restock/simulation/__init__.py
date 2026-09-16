"""Simulation: score the pipeline against a generated world, in rupees.

See docs/simulation_feature_design.md. `run.compare()` is the entry point.
"""

from agentic_restock.simulation.run import EngineRun, compare, run_engine, world
from agentic_restock.simulation.scoring import PairOutcome, Scorecard, score
from agentic_restock.simulation.truth import PairTruth

__all__ = [
    "EngineRun",
    "PairOutcome",
    "PairTruth",
    "Scorecard",
    "compare",
    "run_engine",
    "score",
    "world",
]
