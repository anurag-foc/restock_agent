"""The incumbent ERP rule, run against the same generated world.

The simulation as shipped compares `automatic` against `statsforecast` — two of our own
estimators. That answers an internal question ("which of ours is better") and none of the
question a client has, because they have no reference point for either. The reference point a
client already owns is **their own MRP's reorder-point rule**, and until it is on the chart the
comparison has no origin.

It is also not a strawman we invented. The rule is already written down twice in this repo:

- [`detectors/scanners.py`] names it in its own docstring as the test S1 deliberately does
  *not* use — "the ERP's own test: it adds nothing, it is what produces 8,366 messages a week".
- [`generation/replenishment.py`] drives the generated world's ordering with it, so the world
  these arms are scored on was already replenished by the incumbent logic.

And the incumbent's *forecast* is already in the data. `fact_inventory_snapshot`'s
`AVG_DAILY_CONSUMPTION` — carried into `part_position` as `NAIVE_DAILY_CONSUMPTION` precisely so
"the correction is auditable rather than asserted" — is the flat trailing average an MRP runs on.
S6 (`DEMAND_SHIFT`) exists to fire when corrected burn disagrees with it. So the baseline arm
reconstructs the incumbent from columns the pipeline already carries; it invents no data.

## What is held constant, and what is allowed to differ

This is the whole validity of the comparison, so it is stated rather than implied.

| | source | why |
|---|---|---|
| the world | shared `world()` frames | same pairs, same demand, same suppliers, same day |
| the measured position | one `run.measure()` call | two arms on two independently built position tables compare the construction, not the arms |
| **the trigger** | **each arm's own** | the variable under test |
| **the order quantity** | **each arm's own** | naive burn sizes the incumbent's buy; over- and under-buying is a real cost of the naive method and hiding it would flatter us |
| the price of a unit | shared `fixes.effective_unit_cost` | a difference in the price tag is not a difference in the decision |
| exposure | shared, from `estimators/risk.py` | "truth decides the cell, the product's pricing decides the amount" (`scoring.py`). Both arms judged by one yardstick |

Two ways this arm is deliberately made **stronger** than the real thing, because understating
your own opponent is the only safe direction for a figure that will be shown to a client:

1. **The textbook reorder point is offered, not just the safety-stock breach.**
   `RULE_REORDER_POINT` covers lead-time demand as well as the buffer. `RULE_SAFETY_STOCK` is
   the cruder `available <= safety_stock` that `scanners.py` names. Running both is how we find
   out whether any result is an artefact of which variant we picked.
2. **Only parts with a supplier contract are scanned**, matching S1's universe. A real MRP
   raises planned orders on in-house assemblies too, so this understates the incumbent's alert
   volume. `ErpScan.breaches_unpriceable` reports how many were dropped, so the understatement
   is a disclosed number rather than a silent one.

## No output budget, and no exposure floor

Both are *ours*, and both are the product thesis rather than neutral plumbing:

- `selection.select`'s budget of 4 is not applied — the incumbent emits every breach. That is
  what the alert-volume comparison is measuring.
- S1's `min_exposure` floor is not applied — an MRP has no concept of exposure and cannot skip a
  breach for being small. Applying our floor to their rule would quietly hand them our best idea.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from agentic_restock import settings as st
from agentic_restock.detectors import findings as F
from agentic_restock.detectors import fixes
from agentic_restock.generation import policy
from agentic_restock.money import format_inr as _inr
from agentic_restock.simulation import run as sim_run
from agentic_restock.simulation import truth as sim_truth
from agentic_restock.simulation.scoring import score

RULE_SAFETY_STOCK = "erp_safety_stock"
"""`available <= safety_stock`. The test `scanners.py` names as the ERP's own."""

RULE_REORDER_POINT = "erp_reorder_point"
"""`available <= safety_stock + naive_daily x contracted_lead_days`. The textbook reorder
point, and the rule `generation/replenishment.py` replenishes the generated world with."""

RULES = (RULE_SAFETY_STOCK, RULE_REORDER_POINT)

# A flat average of zero means the ERP's own forecast says this part is not moving. An MRP with
# no forecast raises no planned order, so the rule is silent here. Not a convenience: crediting
# a zero-burn pair would give it a zero-cost fix and therefore full exposure as decision value,
# which is exactly the bug S1's docstring records putting eight in-house assemblies at the top.
MIN_NAIVE_BURN = 1e-9


@dataclass(frozen=True)
class ErpScan:
    """What the incumbent rule produced, and what it was not allowed to produce."""

    findings: list[F.Finding] = field(default_factory=list)
    breaches_total: int = 0
    """Pairs below the reorder point, before the contract restriction."""
    breaches_unpriceable: int = 0
    """Breaches on parts with no supplier contract, dropped to match S1's universe. A real MRP
    would raise these too, so this is the amount by which the arm understates the incumbent."""


def reorder_point(row, rule: str) -> float:
    """The level the incumbent triggers at, in units."""
    safety = float(getattr(row, "SAFETY_STOCK_QTY", 0) or 0)
    if rule == RULE_SAFETY_STOCK:
        return safety
    naive = float(getattr(row, "NAIVE_DAILY_CONSUMPTION", 0.0) or 0.0)
    lead = float(getattr(row, "CONTRACTED_LEAD_DAYS", 0.0) or 0.0)
    return safety + naive * lead


def _erp_purchase(
    *,
    part_id: str,
    supplier_id: str,
    available_qty: int,
    max_stock_level: int,
    naive_burn: float,
    moq: int,
    pack_size: int,
    effective_unit_cost_per_unit: float,
    unit_cost: float,
    holding_rate: float,
) -> dict:
    """The incumbent's own order quantity, priced with our unit economics.

    Deliberately not `fixes.build_purchase_option`. FX3 computes the quantity from *our* target
    cover and *our* corrected burn — its docstring is emphatic that the quantity is computed and
    never chosen, because letting a caller supply one is what silently disabled the MOQ
    constraint. Rather than add a caller-supplied-quantity path to FX3 for the sake of a
    baseline, the incumbent's sizing is written out here: order up to `MAX_STOCK_LEVEL`, the
    ERP's own target level, respecting MOQ and pack size — which is exactly what
    `generation/replenishment.py::_order_quantity` does.

    The excess is then charged at the same holding rate over the naive burn, so an overbuy the
    flat average causes is priced the same way an overbuy our estimate causes would be.
    """
    required = max(int(max_stock_level) - int(available_qty), 0)
    pack = max(int(pack_size), 1)
    orderable = max(int(moq), int(math.ceil(required / pack) * pack)) if required > 0 else 0

    excess = max(orderable - required, 0)
    excess_months = (excess / naive_burn / 30.0) if naive_burn > MIN_NAIVE_BURN else 0.0
    holding = policy.excess_holding_cost(excess, unit_cost, naive_burn, holding_rate)

    subtotal = orderable * float(effective_unit_cost_per_unit)
    return {
        "max_stock_level": int(max_stock_level),
        "available_qty": int(available_qty),
        "required_qty": required,
        "moq": int(moq),
        "pack_size": pack,
        "orderable_qty": orderable,
        "excess_qty": excess,
        "excess_months": round(excess_months, 1),
        "excess_holding_cost": round(holding, 2),
        "effective_unit_cost": round(float(effective_unit_cost_per_unit), 2),
        "subtotal": round(subtotal, 2),
        "action_cost": round(subtotal + holding, 2),
    }


def scan_erp(
    part_position: pd.DataFrame,
    supplier_performance: pd.DataFrame | None = None,
    *,
    rule: str = RULE_REORDER_POINT,
    settings: st.Settings | None = None,
) -> ErpScan:
    """Every pair the incumbent rule would raise, priced on the shared yardstick.

    Emits `STOCKOUT_RISK` findings because that is honestly what the rule is: a stockout
    trigger. The finding shape matters only in that `scoring.py` reads `part_id`,
    `warehouse_id`, `exposure` and `action_cost` off it.
    """
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}; expected one of {RULES}")

    cfg = settings or st.DEFAULTS
    preferred: dict[str, dict] = {}
    if supplier_performance is not None and not supplier_performance.empty:
        preferred = (
            supplier_performance[supplier_performance["IS_PREFERRED"]]
            .set_index("PART_ID")
            .to_dict("index")
        )

    out: list[F.Finding] = []
    breaches = 0
    unpriceable = 0

    for row in part_position.itertuples(index=False):
        trigger = reorder_point(row, rule)
        available = float(row.AVAILABLE_QTY or 0)
        if available > trigger:
            continue

        naive = float(row.NAIVE_DAILY_CONSUMPTION or 0.0)
        if naive <= MIN_NAIVE_BURN:
            continue  # the ERP's own forecast says nothing is moving; no planned order

        breaches += 1

        contract = preferred.get(row.PART_ID)
        if contract is None:
            unpriceable += 1  # built in-house; disclosed rather than counted
            continue

        effective, _premium = fixes.effective_unit_cost(
            quoted_unit_cost=float(contract["CONTRACT_UNIT_COST"]),
            reject_rate=float(contract["REJECT_RATE"]),
            sigma_lead_days=float(contract["SIGMA_LEAD_DAYS"]),
            # The incumbent's own view of consumption, not ours. The variance premium exists to
            # price an unreliable supplier against how fast the part actually moves, and using
            # our corrected burn here would lend the ERP an estimate it does not have.
            forward_burn=naive,
            unit_cost=float(row.UNIT_COST),
            criticality_class=row.CRITICALITY_CLASS,
            holding_rate=cfg.holding_rate,
        )
        purchase = _erp_purchase(
            part_id=row.PART_ID,
            supplier_id=contract["SUPPLIER_ID"],
            available_qty=int(row.AVAILABLE_QTY),
            max_stock_level=int(row.MAX_STOCK_LEVEL or 0),
            naive_burn=naive,
            moq=int(contract["MOQ"]),
            pack_size=int(contract["PACK_SIZE"]),
            effective_unit_cost_per_unit=effective,
            unit_cost=float(row.UNIT_COST),
            holding_rate=cfg.holding_rate,
        )

        exposure = float(row.EXPOSURE or 0.0)
        out.append(
            F.Finding(
                finding_type=F.STOCKOUT_RISK,
                subject_type=F.SUBJECT_PART_WAREHOUSE,
                subject_id=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                part_id=row.PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                supplier_id=contract["SUPPLIER_ID"],
                exposure=exposure,
                p_stockout=float(row.P_STOCKOUT or 0.0),
                consequence=float(row.CONSEQUENCE or 0.0),
                action_type=F.ACTION_PURCHASE,
                action_detail=(
                    f"buy {purchase['orderable_qty']} units of {row.PART_ID} from "
                    f"{contract['SUPPLIER_ID']} for {row.WAREHOUSE_ID}"
                ),
                action_cost=float(purchase["action_cost"]),
                exposure_basis=(
                    f"stock {int(available)} at or below reorder point "
                    f"{trigger:.0f}; exposure {_inr(exposure)} priced on the shared yardstick, "
                    f"not by the rule itself"
                ),
                confidence="LOW",
                suppression_key=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                evidence={
                    "rule": rule,
                    "available_qty": int(available),
                    "safety_stock_qty": int(row.SAFETY_STOCK_QTY or 0),
                    "naive_daily_consumption": round(naive, 3),
                    "contracted_lead_days": round(
                        float(row.CONTRACTED_LEAD_DAYS or 0.0), 1
                    ),
                    "reorder_point": round(trigger, 1),
                    "purchase": purchase,
                },
                assumptions_used=F.assumption_values([F.ASSUMPTION_HOLDING_RATE]),
            )
        )

    return ErpScan(
        findings=out, breaches_total=breaches, breaches_unpriceable=unpriceable
    )


def run_erp(
    rule: str,
    measures: sim_run.Measures,
    truth: dict,
    *,
    settings: st.Settings | None = None,
) -> tuple[sim_run.EngineRun, ErpScan]:
    """Score the incumbent rule on an already-measured world.

    Takes `Measures` rather than building its own, so the arm is scored against byte-identical
    positions to the engine arms. `detected == selected`: there is no output budget, which is
    the point.
    """
    scan = scan_erp(
        measures.part_position,
        measures.supplier_performance,
        rule=rule,
        settings=settings,
    )
    card = score(measures.part_position, truth, scan.findings, scan.findings)
    return (
        sim_run.EngineRun(
            engine=rule,
            budget=0,  # 0 = no budget, every breach emitted
            scorecard=card,
            detected=scan.findings,
            selected=scan.findings,
            truth=truth,
        ),
        scan,
    )


def truth_for(measures: sim_run.Measures, world_frames: dict[str, pd.DataFrame]) -> dict:
    """Realised-shortage truth for the world these measures were built from.

    Scoped to the purchasable universe, which is the same restriction `scan_erp` and S1 both
    already apply to themselves. Scoring over a universe neither arm scans measured neither.
    """
    return sim_truth.build(
        world_frames["position"], sim_truth.purchasable_parts(measures.supplier_performance)
    )


def run_all(
    *,
    settings: st.Settings | None = None,
    budget: int | None = None,
    world_frames: dict[str, pd.DataFrame] | None = None,
) -> list[sim_run.EngineRun]:
    """Both incumbent rules against one world, ready to sit beside the engine runs.

    Measures the world once and scores both rules against it, so the ERP arms and the engine
    arms in the same batch are comparing decisions rather than two separately built position
    tables. The `automatic` engine's settings are used to build those measures: the incumbent
    reads only its own columns (recorded average, safety stock, contracted lead time) for the
    trigger, and takes exposure from the shared yardstick, so which engine built the table does
    not reach its decisions.
    """
    f = world_frames if world_frames is not None else sim_run.world()
    cfg = sim_run._settings_for("automatic", settings, budget)
    measures = sim_run.measure(cfg, f)
    truth = truth_for(measures, f)
    return [run_erp(rule, measures, truth, settings=settings)[0] for rule in RULES]
