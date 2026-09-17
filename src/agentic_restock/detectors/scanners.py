"""S1-S8 — the eight scanners, each at its own natural grain.

This is the fix for the structural defect. The superseded ranking derived `signal_type` from one
mutually-exclusive `CASE` over a (part, warehouse) row, so it could produce exactly two
categories — and `STALLED_COMMITMENT` was a relabel of those two rather than a third. Six nuances
became columns that could never be *the reason* a candidate was raised, including lateral
transfer, which the evidence base calls the strongest opportunity in the product.

Here each scanner runs where its nuance actually lives:

    S1 stockout risk      (part, warehouse)
    S2 cascade block      (parent, warehouse)      <- rolled up, value counted once
    S3 redeployment       (part) across the network
    S4 dead capital       (part, warehouse)
    S5 lead-time signal   (supplier)               <- once, not once per part
    S6 demand shift       (part, warehouse)
    S7 supplier economics (supplier, part)
    S8 MOQ uneconomic     (part, supplier)

The primary stockout test is `days_of_cover < effective_lead + review_period`, not
`on_hand < safety_stock`. The latter is the ERP's own test: it adds nothing, it is what produces
8,366 messages a week, and it says nothing about whether you will actually be hurt. The
time-based test says "you will run out before a replenishment can land", which is a decision
rather than a threshold crossing — and it makes the burn and lead-time estimators structurally
load-bearing for *detection* rather than narrative garnish.
"""

from __future__ import annotations

import pandas as pd

from agentic_restock import settings as st
from agentic_restock.detectors import findings as F
from agentic_restock.detectors import fixes
from agentic_restock.estimators import burn as e1
from agentic_restock.generation import policy
from agentic_restock.money import format_inr as _inr

# --- thresholds ------------------------------------------------------------

# S1: a stockout probability worth raising at all.
MIN_P_STOCKOUT = 0.20

# S1/S3: below this exposure a finding is noise however true it is. Scarcity is the feature.
MIN_EXPOSURE = 25_000.0

# S4: dead capital. Both must hold -- deep cover AND no recent movement. Cover alone flags a slow
# mover that is still being consumed.
DEAD_CAPITAL_COVER_DAYS = 180.0
DEAD_CAPITAL_MIN_VALUE = 50_000.0

# S5: a supplier's lead time is worth reporting when the mean has drifted OR the spread is wide.
# Both, because zero drift with a 14-day spread is unmanageable and a mean-only test sees nothing.
LEADTIME_DRIFT_DAYS = 4.0
LEADTIME_CV_THRESHOLD = 0.20
LEADTIME_MIN_OBSERVATIONS = 3

# S6: a demand shift must be sustained, and the buffer must not have moved with it.
DEMAND_SHIFT_RATIO = 1.25
DEMAND_SHIFT_MIN_COVER_LOSS = 0.75

# S7: only worth raising when switching supplier saves something material. An absolute figure,
# not a share of spend: a share scales with how much you buy, so the highest-volume parts -- where
# a small per-unit saving is worth the most -- needed the largest saving before anything surfaced.
MIN_SUPPLIER_SAVING = 25_000.0


def _cover_threshold(row) -> float:
    """`mu_lead + review period` — the horizon a shortage has to be judged against."""
    return float(row.MU_LEAD_DAYS or 30.0) + policy.REVIEW_PERIOD_DAYS


# ---------------------------------------------------------------------------
# S1 -- stockout risk
# ---------------------------------------------------------------------------


def scan_stockout_risk(
    part_position: pd.DataFrame, supplier_performance: pd.DataFrame | None = None,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Parts that will run out before a replenishment can land, with the buy priced.

    The fix is constructed here rather than left to the report, because `action_cost` has to be a
    real figure: `orderable_qty x effective_unit_cost + excess_holding`. Without it
    `decision_value` collapses to exposure and the ranking cannot distinguish a cheap fix from
    an expensive one -- which is the whole basis of ranking by what changes if a human acts.

    **Only purchasable parts.** Assemblies and sub-assemblies are built in-house and have no
    contract, so "will it run out before a replenishment lands" has no replenishment to speak
    of -- and their shortage is already the subject of S2's cascade at the parent. Scanning them
    here gave them exposure with a zero action cost, which put eight in-house assemblies at the
    top of the ranking with a decision value equal to their exposure.
    """
    cfg = settings or st.DEFAULTS
    preferred: dict[str, dict] = {}
    if supplier_performance is not None and not supplier_performance.empty:
        preferred = (
            supplier_performance[supplier_performance["IS_PREFERRED"]]
            .set_index("PART_ID")
            .to_dict("index")
        )

    out: list[F.Finding] = []
    for row in part_position.itertuples(index=False):
        contract = preferred.get(row.PART_ID)
        if contract is None:
            continue  # built in-house; S2 owns its shortage

        cover = row.DAYS_OF_COVER
        if cover is None or pd.isna(cover):
            continue  # nothing moving; that is S4's question, not this one
        if cover >= _cover_threshold(row):
            continue
        if (row.P_STOCKOUT or 0) < MIN_P_STOCKOUT or (row.EXPOSURE or 0) < cfg.min_exposure:
            continue

        purchase = None
        action_type, action_detail, action_cost = F.ACTION_NONE, "no supplier on contract", 0.0
        if contract is not None:
            effective, _premium = fixes.effective_unit_cost(
                quoted_unit_cost=float(contract["CONTRACT_UNIT_COST"]),
                reject_rate=float(contract["REJECT_RATE"]),
                sigma_lead_days=float(contract["SIGMA_LEAD_DAYS"]),
                forward_burn=float(row.FORWARD_BURN),
                unit_cost=float(row.UNIT_COST),
                criticality_class=row.CRITICALITY_CLASS,
                holding_rate=cfg.holding_rate,
            )
            purchase = fixes.build_purchase_option(
                part_id=row.PART_ID,
                supplier_id=contract["SUPPLIER_ID"],
                target_cover_days=float(row.TARGET_COVER_DAYS),
                forward_burn=float(row.FORWARD_BURN),
                available_qty=int(row.AVAILABLE_QTY),
                moq=int(contract["MOQ"]),
                pack_size=int(contract["PACK_SIZE"]),
                effective_unit_cost_per_unit=effective,
                unit_cost=float(row.UNIT_COST),
                exposure=float(row.EXPOSURE),
                holding_rate=cfg.holding_rate,
            )
            action_type = F.ACTION_PURCHASE
            action_detail = (
                f"buy {purchase.orderable_qty} units of {row.PART_ID} from "
                f"{contract['SUPPLIER_ID']} for {row.WAREHOUSE_ID}"
            )
            action_cost = purchase.action_cost

        out.append(
            F.Finding(
                finding_type=F.STOCKOUT_RISK,
                exposure_basis=(
                    f"{float(row.P_STOCKOUT) * 100:.0f}% chance of running out "
                    f"x {_inr(float(row.CONSEQUENCE))} it costs if it does "
                    f"≈ {_inr(float(row.EXPOSURE))} "
                    f"({str(row.CONSEQUENCE_BASIS).replace('_', ' ').lower()})"
                ),
                subject_type=F.SUBJECT_PART_WAREHOUSE,
                subject_id=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                part_id=row.PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                supplier_id=row.PREFERRED_SUPPLIER_ID,
                exposure=float(row.EXPOSURE),
                p_stockout=float(row.P_STOCKOUT),
                consequence=float(row.CONSEQUENCE),
                action_type=action_type,
                action_detail=action_detail,
                action_cost=action_cost,
                confidence=row.BURN_CONFIDENCE,
                suppression_key=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                evidence={
                    **({"purchase": purchase.evidence} if purchase else {}),
                    "available_qty": int(row.AVAILABLE_QTY),
                    "on_hand_qty": int(row.ON_HAND_QTY),
                    "in_transit_qty": int(row.IN_TRANSIT_QTY),
                    "forward_burn": round(float(row.FORWARD_BURN), 2),
                    "days_of_cover": round(float(cover), 1),
                    "mu_lead_days": round(float(row.MU_LEAD_DAYS or 0), 1),
                    "sigma_lead_days": round(float(row.SIGMA_LEAD_DAYS or 0), 2),
                    "cover_threshold_days": round(_cover_threshold(row), 1),
                    "p_stockout": round(float(row.P_STOCKOUT), 3),
                    "consequence": round(float(row.CONSEQUENCE), 2),
                    "consequence_basis": row.CONSEQUENCE_BASIS,
                    "burn_method": row.BURN_METHOD,
                    "burn_confidence": row.BURN_CONFIDENCE,
                    "lead_tier": row.LEAD_TIER,
                },
                assumptions_used=F.assumption_values(
                    [F.ASSUMPTION_REVIEW_PERIOD, F.ASSUMPTION_SERVICE_LEVEL]
                    + ([F.ASSUMPTION_HOLDING_RATE] if purchase else [])
                    + (
                        [F.ASSUMPTION_CONSEQUENCE_PROXY]
                        if row.CONSEQUENCE_BASIS == "CRITICALITY_PROXY"
                        else []
                    ),
                    criticality_class=row.CRITICALITY_CLASS,
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# S2 -- cascade block
# ---------------------------------------------------------------------------


def scan_cascade_block(
    parent_cascades: pd.DataFrame,
    part_position: pd.DataFrame | None = None,
    supplier_performance: pd.DataFrame | None = None,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Assemblies that cannot meet their plan, one finding per parent, with the fix priced.

    The action names the **whole binding set**. "Buy component X and ₹6.5 cr is unblocked" is
    false when Y binds at the same level, and a PM who acts on it will find the line still
    stopped.

    `action_cost` is the cost of buying every binding child's shortfall. Leaving it at zero made
    `decision_value` equal exposure for the largest findings in the set, which held the top of
    the ranking identical between the two orderings and hid whether decision value changes
    anything where it matters.
    """
    cfg = settings or st.DEFAULTS
    cost_of = _binding_child_cost_lookup(part_position, supplier_performance, cfg)

    out: list[F.Finding] = []
    for row in parent_cascades.itertuples(index=False):
        if row.UNITS_BLOCKED <= 0:
            continue

        binding = [c for c in (row.BINDING_CHILDREN or "").split(",") if c]
        fix_cost = sum(
            cost_of(child, row.WAREHOUSE_ID, int(row.UNITS_BLOCKED)) for child in binding
        )
        detail = (
            f"unblock {row.PARENT_PART_ID}: needs "
            + " AND ".join(binding)
            + (" (all of them — fixing one leaves it blocked)" if len(binding) > 1 else "")
        )

        out.append(
            F.Finding(
                finding_type=F.CASCADE_BLOCK,
                subject_type=F.SUBJECT_PARENT_WAREHOUSE,
                subject_id=f"{row.PARENT_PART_ID}@{row.WAREHOUSE_ID}",
                part_id=row.PARENT_PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                exposure=float(row.VALUE_AT_RISK),
                consequence=float(row.VALUE_AT_RISK),
                exposure_basis=(
                    f"{int(row.UNITS_BLOCKED)} assemblies that cannot be built "
                    f"x {_inr(float(row.PARENT_UNIT_COST))} each "
                    f"= {_inr(float(row.VALUE_AT_RISK))}"
                ),
                action_type=F.ACTION_PURCHASE,
                action_detail=detail,
                action_cost=fix_cost,
                confidence="HIGH",
                suppression_key=f"{row.PARENT_PART_ID}@{row.WAREHOUSE_ID}",
                evidence={
                    "required_units": int(row.REQUIRED_UNITS),
                    "parent_on_hand_qty": int(row.PARENT_ON_HAND_QTY),
                    "buildable_from_children": int(row.BUILDABLE_FROM_CHILDREN),
                    "supply_units": int(row.SUPPLY_UNITS),
                    "units_blocked": int(row.UNITS_BLOCKED),
                    "parent_unit_cost": round(float(row.PARENT_UNIT_COST), 2),
                    "value_at_risk": round(float(row.VALUE_AT_RISK), 2),
                    "binding_children": binding,
                    "binding_child_count": int(row.BINDING_CHILD_COUNT),
                    "fix_cost_all_binding_children": round(fix_cost, 2),
                },
                assumptions_used=F.assumption_values(
                    [F.ASSUMPTION_CASCADE_HORIZON]
                    + ([F.ASSUMPTION_HOLDING_RATE] if fix_cost > 0 else [])
                ),
            )
        )
    return out


def _binding_child_cost_lookup(
    part_position: pd.DataFrame | None,
    supplier_performance: pd.DataFrame | None,
    cfg: st.Settings,
):
    """Return a callable pricing the purchase of `units` more of a binding child.

    Priced through FX3 so the MOQ and pack constraints apply here too -- unblocking a line still
    means placing a real order, and a fix cost that ignored the minimum order would understate
    it exactly where the order is small.
    """
    if part_position is None or supplier_performance is None or supplier_performance.empty:
        return lambda _child, _warehouse, _units: 0.0

    preferred = (
        supplier_performance[supplier_performance["IS_PREFERRED"]]
        .set_index("PART_ID")
        .to_dict("index")
    )
    positions = {
        (r.PART_ID, r.WAREHOUSE_ID): r for r in part_position.itertuples(index=False)
    }

    def cost(child: str, warehouse_id: str, units: int) -> float:
        row = positions.get((child, warehouse_id))
        contract = preferred.get(child)
        if row is None or contract is None or units <= 0:
            return 0.0
        effective, _ = fixes.effective_unit_cost(
            quoted_unit_cost=float(contract["CONTRACT_UNIT_COST"]),
            reject_rate=float(contract["REJECT_RATE"]),
            sigma_lead_days=float(contract["SIGMA_LEAD_DAYS"]),
            forward_burn=float(row.FORWARD_BURN),
            unit_cost=float(row.UNIT_COST),
            criticality_class=row.CRITICALITY_CLASS,
            holding_rate=cfg.holding_rate,
        )
        pack = max(int(contract["PACK_SIZE"]), 1)
        orderable = max(
            int(contract["MOQ"]), int(-(-units // pack) * pack)
        )
        return orderable * effective

    return cost


# ---------------------------------------------------------------------------
# S3 -- redeployment
# ---------------------------------------------------------------------------


def scan_redeployment(
    part_position: pd.DataFrame, supplier_performance: pd.DataFrame,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Stock worth more somewhere else than where it is. A matching problem, not a threshold.

    The highest-ROI action in the evidence base (§2: up to 50% lower provisioning cost, zero cash
    outlay, verifiable the same week) and one the superseded design could never raise as a
    finding — it could only cheapen the fix for a candidate that got there another way.
    """
    cfg = settings or st.DEFAULTS
    freight = (
        supplier_performance.groupby("PART_ID")["MEAN_FREIGHT_COST"].mean().to_dict()
        if not supplier_performance.empty
        else {}
    )

    out: list[F.Finding] = []
    for part_id, group in part_position.groupby("PART_ID"):
        if len(group) < 2:
            continue

        rows = [
            {
                "warehouse_id": r.WAREHOUSE_ID,
                "available_qty": int(r.AVAILABLE_QTY),
                "safety_stock_qty": int(r.SAFETY_STOCK_QTY),
                "forward_burn": float(r.FORWARD_BURN),
                "sigma_d": float(r.SIGMA_D),
                "mu_lead": float(r.MU_LEAD_DAYS or 30.0),
                "sigma_lead": float(r.SIGMA_LEAD_DAYS or 0.0),
                "consequence": float(r.CONSEQUENCE),
                "days_of_cover": r.DAYS_OF_COVER,
                "exposure": float(r.EXPOSURE),
                "criticality_class": r.CRITICALITY_CLASS,
            }
            for r in group.itertuples(index=False)
        ]

        for receiver in rows:
            cover = receiver["days_of_cover"]
            if cover is None or pd.isna(cover):
                continue
            if cover >= receiver["mu_lead"] * fixes.NEEDY_COVER_MULTIPLE:
                continue
            if receiver["exposure"] < cfg.min_exposure:
                continue

            donors_by_id = {r["warehouse_id"]: r for r in rows}
            options = fixes.rank_transfer_options(
                part_id=part_id,
                receiver=receiver,
                candidates=rows,
                freight_cost=float(freight.get(part_id, 0.0)),
                settings=cfg,
            )
            if not options or options[0].benefit <= 0:
                continue

            best = options[0]
            out.append(
                F.Finding(
                    finding_type=F.REDEPLOYMENT,
                    subject_type=F.SUBJECT_PART_NETWORK,
                    subject_id=f"{part_id}:{best.donor_warehouse_id}->{best.receiver_warehouse_id}",
                    part_id=part_id,
                    warehouse_id=best.receiver_warehouse_id,
                    exposure=float(best.benefit),
                    p_stockout=best.receiver_risk_before,
                    consequence=receiver["consequence"],
                    exposure_basis=_transfer_basis(best, receiver, donors_by_id),
                    action_type=F.ACTION_TRANSFER,
                    action_detail=(
                        f"transfer {best.transfer_qty} units of {part_id} from "
                        f"{best.donor_warehouse_id} to {best.receiver_warehouse_id}"
                    ),
                    action_cost=best.action_cost,
                    confidence="MEDIUM",
                    suppression_key=f"{part_id}@{best.receiver_warehouse_id}",
                    evidence={
                        **best.evidence,
                        "donors_considered": len(options),
                        "runner_up_donor": (
                            options[1].donor_warehouse_id if len(options) > 1 else None
                        ),
                        "receiver_risk_before": round(best.receiver_risk_before, 3),
                        "receiver_risk_after": round(best.receiver_risk_after, 3),
                        "donor_risk_before": round(best.donor_risk_before, 3),
                        "donor_risk_after": round(best.donor_risk_after, 3),
                    },
                    # No holding rate: a transfer buys nothing and holds nothing extra.
                    assumptions_used=F.assumption_values(
                        [F.ASSUMPTION_SERVICE_LEVEL],
                        criticality_class=receiver["criticality_class"],
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# S4 -- dead capital
# ---------------------------------------------------------------------------


def scan_dead_capital(
    part_position: pd.DataFrame, settings: st.Settings | None = None
) -> list[F.Finding]:
    """Stock that will never be consumed. The excess half of the network view.

    The superseded design detects only *shortage*; §2's evidence is that 38% of inventory is
    excess and $1.7 trillion of working capital is trapped. This is the same network view pointed
    the other way, and no incumbent surfaces it.
    """
    cfg = settings or st.DEFAULTS
    out: list[F.Finding] = []
    for row in part_position.itertuples(index=False):
        cover = row.DAYS_OF_COVER
        stopped = row.BURN_METHOD == "STOPPED" or float(row.FORWARD_BURN) <= 0
        deep = cover is not None and not pd.isna(cover) and cover > cfg.dead_stock_cover_days

        if not (stopped or deep):
            continue

        trapped = int(row.ON_HAND_QTY) * float(row.UNIT_COST)
        if trapped < cfg.dead_stock_min_value:
            continue

        annual_carry = trapped * cfg.holding_rate
        out.append(
            F.Finding(
                finding_type=F.DEAD_CAPITAL,
                subject_type=F.SUBJECT_PART_WAREHOUSE,
                subject_id=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                part_id=row.PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                # Exposure is the carrying cost, not the stock value: the capital is not lost,
                # it is idle. Quoting the whole stock value as "at risk" would overstate it.
                exposure=annual_carry,
                consequence=annual_carry,
                exposure_basis=(
                    f"{_inr(trapped)} of stock sitting still "
                    f"x {cfg.holding_rate * 100:.0f}% a year to hold it "
                    f"= {_inr(annual_carry)} a year — a recurring cost, not a one-off loss"
                ),
                action_type=F.ACTION_REVIEW_STOCK,
                action_detail=(
                    f"review {int(row.ON_HAND_QTY)} units of {row.PART_ID} at "
                    f"{row.WAREHOUSE_ID} — no longer moving"
                    if stopped
                    else f"review {int(row.ON_HAND_QTY)} units of {row.PART_ID} at "
                    f"{row.WAREHOUSE_ID} — {round(float(cover))} days of cover"
                ),
                confidence=row.BURN_CONFIDENCE,
                suppression_key=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                evidence={
                    "on_hand_qty": int(row.ON_HAND_QTY),
                    "unit_cost": round(float(row.UNIT_COST), 2),
                    "trapped_value": round(trapped, 2),
                    "holding_rate_pct": round(cfg.holding_rate * 100, 1),
                    "forward_burn": round(float(row.FORWARD_BURN), 3),
                    "days_of_cover": None if stopped else round(float(cover), 1),
                    "burn_method": row.BURN_METHOD,
                    "annual_carrying_cost": round(annual_carry, 2),
                },
                assumptions_used=F.assumption_values([F.ASSUMPTION_HOLDING_RATE]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# S5 -- lead-time signal
# ---------------------------------------------------------------------------


def scan_leadtime_signal(
    supplier_performance: pd.DataFrame, part_position: pd.DataFrame,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Suppliers whose real behaviour differs from their contract — **once per supplier**.

    Reported at supplier grain with the affected parts attached. One drifting supplier reported
    once per part is how a single problem becomes twelve alerts, which is the alert-fatigue
    failure in miniature.

    Fires on drift OR on spread. A supplier exactly on contract with a 14-day spread is
    unmanageable, and a mean-only test sees nothing wrong with it.
    """
    cfg = settings or st.DEFAULTS
    if supplier_performance.empty:
        return []

    spend = (
        part_position.groupby("PREFERRED_SUPPLIER_ID")
        .apply(
            lambda g: float((g["FORWARD_BURN"] * g["UNIT_COST"] * 365).sum()), include_groups=False
        )
        .to_dict()
        if not part_position.empty
        else {}
    )

    out: list[F.Finding] = []
    measured = supplier_performance[supplier_performance["IS_MEASURED"]]
    for supplier_id, group in measured.groupby("SUPPLIER_ID"):
        observations = int(group["LEAD_OBSERVATIONS"].max())
        if observations < LEADTIME_MIN_OBSERVATIONS:
            continue

        drift = float(group["DRIFT_DAYS"].mean())
        mu = float(group["MU_LEAD_DAYS"].mean())
        sigma = float(group["SIGMA_LEAD_DAYS"].mean())
        cv = sigma / mu if mu > 0 else 0.0

        drifting = abs(drift) >= LEADTIME_DRIFT_DAYS
        erratic = cv >= LEADTIME_CV_THRESHOLD
        if not (drifting or erratic):
            continue

        annual_spend = float(spend.get(supplier_id, 0.0))
        # The buffer this supplier's spread forces, carried for a year.
        parts = sorted(group["PART_ID"].tolist())
        exposure = annual_spend * cfg.holding_rate * (cv if erratic else 0.0) + (
            annual_spend * (max(drift, 0.0) / 365.0)
        )
        if exposure < cfg.min_exposure:
            continue

        reason = (
            "erratic: on contract on average but unpredictable"
            if erratic and not drifting
            else "consistently late"
            if drifting and not erratic
            else "late and unpredictable"
        )

        out.append(
            F.Finding(
                finding_type=F.LEADTIME_SIGNAL,
                exposure_basis=_leadtime_basis(
                    annual_spend, cv, drift, erratic, exposure, cfg.holding_rate
                ),
                subject_type=F.SUBJECT_SUPPLIER,
                subject_id=supplier_id,
                supplier_id=supplier_id,
                exposure=exposure,
                action_type=F.ACTION_RECALIBRATE,
                action_detail=(
                    f"{supplier_id}: {reason} — contracted "
                    f"{float(group['CONTRACTED_LEAD_DAYS'].mean()):.0f} days, "
                    f"actual {mu:.0f} ± {sigma:.0f}; affects {len(parts)} part(s)"
                ),
                confidence="HIGH" if observations >= 8 else "MEDIUM",
                suppression_key=f"supplier:{supplier_id}",
                evidence={
                    "contracted_lead_days": round(float(group["CONTRACTED_LEAD_DAYS"].mean()), 1),
                    "observed_mu_lead_days": round(mu, 2),
                    "observed_sigma_lead_days": round(sigma, 2),
                    "drift_days": round(drift, 2),
                    "coefficient_of_variation": round(cv, 3),
                    "p90_lead_days": round(float(group["P90_LEAD_DAYS"].mean()), 1),
                    "otd_rate": round(float(group["OTD_RATE"].mean()), 3),
                    "observations": observations,
                    "lead_tier": group["LEAD_TIER"].iloc[0],
                    "affected_parts": parts,
                    "annual_spend": round(annual_spend, 2),
                },
                assumptions_used=F.assumption_values([F.ASSUMPTION_HOLDING_RATE]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# S6 -- demand shift
# ---------------------------------------------------------------------------


def scan_demand_shift(
    part_position: pd.DataFrame, settings: st.Settings | None = None
) -> list[F.Finding]:
    """Demand has moved and the buffer has not.

    Tests the pair's consumption **now against its own consumption before** — the last 90 days
    against the 90 days ending 180 days ago — and fires when the rate has moved and the safety
    stock has not moved with it. That is §4's stale-master-data wedge measured rather than
    asserted.

    **It used to compare the corrected burn against the snapshot's recorded flat average**, and
    that was wrong in both directions. The recorded average is not a baseline: it is computed
    over a window that absorbs the very change being looked for, so a 1.6x step planted 120 days
    ago showed a gap of only 1.16x and was missed. And on an intermittent part the two numbers
    differ by construction — Croston estimates a lumpy rate differently from a flat daily mean —
    so a spare with 92% zero days showed 1.78x and was reported as a demand surge when nothing
    had changed at all. Both are recorded in estimators/burn.py::DemandShift.

    **Intermittent parts are excluded outright.** The dataset spec names DEMAND_SHIFT among the
    scanners that must stay silent on them (scenarios.py F9), and a before/after mean on a series
    that is 90% zeroes is a comparison of which weeks happened to contain an order.
    """
    cfg = settings or st.DEFAULTS
    out: list[F.Finding] = []
    for row in part_position.itertuples(index=False):
        if not bool(getattr(row, "SHIFT_OBSERVABLE", False)):
            continue
        if float(row.ZERO_DAY_FRACTION or 0.0) >= e1.INTERMITTENCY_THRESHOLD:
            continue

        prior = float(row.PRIOR_DAILY_RATE or 0.0)
        recent = float(row.RECENT_DAILY_RATE or 0.0)
        ratio = float(row.OBSERVED_SHIFT_RATIO or 1.0)
        if ratio < DEMAND_SHIFT_RATIO:
            continue

        # The buffer was sized for the old rate. At the new one it covers `1/ratio` as long, so
        # holding the same days of cover needs `safety_stock x (ratio - 1)` more units.
        cover_at_prior = float(row.SAFETY_STOCK_QTY) / prior if prior > 0 else 0.0
        cover_at_recent = float(row.SAFETY_STOCK_QTY) / recent if recent > 0 else 0.0
        if cover_at_recent > cover_at_prior * DEMAND_SHIFT_MIN_COVER_LOSS:
            continue

        shortfall_units = max(round(float(row.SAFETY_STOCK_QTY) * (ratio - 1.0)), 0)
        exposure = shortfall_units * float(row.UNIT_COST)
        if exposure < cfg.min_exposure:
            continue

        naive = float(row.NAIVE_DAILY_CONSUMPTION or 0.0)
        corrected = float(row.FORWARD_BURN)

        out.append(
            F.Finding(
                finding_type=F.DEMAND_SHIFT,
                exposure_basis=(
                    f"{shortfall_units} units short of the safety stock this faster burn calls "
                    f"for x {_inr(float(row.UNIT_COST))} each = {_inr(exposure)}"
                ),
                subject_type=F.SUBJECT_PART_WAREHOUSE,
                subject_id=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                part_id=row.PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                exposure=exposure,
                action_type=F.ACTION_RECALIBRATE,
                action_detail=(
                    f"raise safety stock for {row.PART_ID} at {row.WAREHOUSE_ID}: "
                    f"demand is {ratio:.1f}x what it was six months ago"
                ),
                confidence=row.BURN_CONFIDENCE,
                suppression_key=f"{row.PART_ID}@{row.WAREHOUSE_ID}",
                evidence={
                    "prior_daily_rate": round(prior, 3),
                    "recent_daily_rate": round(recent, 3),
                    "ratio": round(ratio, 2),
                    "comparison_windows": (
                        f"last {e1.SHIFT_RECENT_WINDOW_DAYS} days vs the "
                        f"{e1.SHIFT_PRIOR_WINDOW_DAYS} days ending "
                        f"{e1.SHIFT_RECENT_WINDOW_DAYS + e1.SHIFT_GAP_DAYS} days ago"
                    ),
                    "recorded_daily_consumption": round(naive, 2),
                    "corrected_forward_burn": round(corrected, 2),
                    # "demand is 1.4x the recorded average" is not decidable without knowing how
                    # much is actually on the shelf -- and the persisted line reads its
                    # CURRENT_STOCK_QTY from here.
                    "on_hand_qty": int(row.ON_HAND_QTY),
                    "safety_stock_qty": int(row.SAFETY_STOCK_QTY),
                    "cover_at_prior_rate_days": round(cover_at_prior, 1),
                    "cover_at_recent_rate_days": round(cover_at_recent, 1),
                    "safety_stock_shortfall_units": shortfall_units,
                    "unit_cost": float(row.UNIT_COST),
                    "burn_method": row.BURN_METHOD,
                    "burn_confidence": row.BURN_CONFIDENCE,
                },
                assumptions_used=F.assumption_values(
                    [F.ASSUMPTION_SERVICE_LEVEL], criticality_class=row.CRITICALITY_CLASS
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# S7 -- supplier economics
# ---------------------------------------------------------------------------


def scan_supplier_economics(
    part_position: pd.DataFrame, supplier_performance: pd.DataFrame,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Parts where the cheapest quote is not the cheapest supplier.

    A 0-100 reliability score is unarguable. A reversed cost ranking is not — and this only fires
    where switching actually saves something, so it is a sourcing decision rather than a
    scoreboard.
    """
    cfg = settings or st.DEFAULTS
    if supplier_performance.empty:
        return []

    burn = {
        (r.PART_ID): (float(r.FORWARD_BURN), float(r.UNIT_COST), r.CRITICALITY_CLASS)
        for r in part_position.itertuples(index=False)
    }

    out: list[F.Finding] = []
    for part_id, group in supplier_performance.groupby("PART_ID"):
        if len(group) < 2 or part_id not in burn:
            continue

        forward_burn, unit_cost, criticality = burn[part_id]
        ranked = fixes.rank_suppliers(
            part_id=part_id,
            quotes=[
                {
                    "supplier_id": r.SUPPLIER_ID,
                    "quoted_unit_cost": float(r.CONTRACT_UNIT_COST),
                    "reject_rate": float(r.REJECT_RATE),
                    "sigma_lead_days": float(r.SIGMA_LEAD_DAYS),
                    "lead_tier": r.LEAD_TIER,
                    "observations": int(r.LEAD_OBSERVATIONS),
                }
                for r in group.itertuples(index=False)
            ],
            forward_burn=forward_burn,
            unit_cost=unit_cost,
            criticality_class=criticality,
            holding_rate=cfg.holding_rate,
        )
        if len(ranked) < 2:
            continue

        best = ranked[0]
        current_id = group[group["IS_PREFERRED"]]["SUPPLIER_ID"]
        if current_id.empty:
            continue
        current = next((o for o in ranked if o.supplier_id == current_id.iloc[0]), None)
        if current is None or current.supplier_id == best.supplier_id:
            continue  # already buying from the best one

        annual_units = forward_burn * 365
        saving = (current.effective_unit_cost - best.effective_unit_cost) * annual_units
        if saving < MIN_SUPPLIER_SAVING:
            continue

        out.append(
            F.Finding(
                finding_type=F.SUPPLIER_ECONOMICS,
                subject_type=F.SUBJECT_SUPPLIER_PART,
                subject_id=f"{part_id}/{current.supplier_id}",
                part_id=part_id,
                supplier_id=current.supplier_id,
                exposure=saving,
                action_type=F.ACTION_RESOURCE,
                action_detail=(
                    f"re-source {part_id}: {best.supplier_id} quotes more than "
                    f"{current.supplier_id} but costs less once rejects and lead-time spread "
                    f"are counted"
                ),
                confidence="MEDIUM",
                suppression_key=f"{part_id}/{current.supplier_id}",
                evidence={
                    "current": current.evidence,
                    "recommended": best.evidence,
                    "annual_units": round(annual_units),
                    "annual_saving": round(saving, 2),
                    "quoted_price_ranking_reversed": bool(
                        best.quoted_unit_cost > current.quoted_unit_cost
                    ),
                },
                assumptions_used=F.assumption_values(
                    [F.ASSUMPTION_HOLDING_RATE, F.ASSUMPTION_SERVICE_LEVEL],
                    criticality_class=criticality,
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# S8 -- MOQ uneconomic
# ---------------------------------------------------------------------------


def scan_moq_uneconomic(
    part_position: pd.DataFrame, supplier_performance: pd.DataFrame,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Parts whose minimum order forces an overbuy that costs more than the risk it removes.

    A **different action type**, which is the point: the answer is to renegotiate the pack, not
    to place the order. Nuance 7 as a finding rather than a footnote on a buy.
    """
    cfg = settings or st.DEFAULTS
    if supplier_performance.empty:
        return []

    preferred = (
        supplier_performance[supplier_performance["IS_PREFERRED"]].set_index("PART_ID").to_dict("index")
    )

    out: list[F.Finding] = []
    for row in part_position.itertuples(index=False):
        contract = preferred.get(row.PART_ID)
        if contract is None or float(row.FORWARD_BURN) <= 0:
            continue

        option = fixes.build_purchase_option(
            part_id=row.PART_ID,
            supplier_id=contract["SUPPLIER_ID"],
            target_cover_days=float(row.TARGET_COVER_DAYS),
            forward_burn=float(row.FORWARD_BURN),
            available_qty=int(row.AVAILABLE_QTY),
            moq=int(contract["MOQ"]),
            pack_size=int(contract["PACK_SIZE"]),
            effective_unit_cost_per_unit=float(contract["CONTRACT_UNIT_COST"]),
            unit_cost=float(row.UNIT_COST),
            exposure=float(row.EXPOSURE),
            holding_rate=cfg.holding_rate,
        )
        if not option.is_uneconomic or option.required_qty <= 0:
            continue

        out.append(
            F.Finding(
                finding_type=F.MOQ_UNECONOMIC,
                subject_type=F.SUBJECT_SUPPLIER_PART,
                subject_id=f"{row.PART_ID}/{contract['SUPPLIER_ID']}",
                part_id=row.PART_ID,
                warehouse_id=row.WAREHOUSE_ID,
                supplier_id=contract["SUPPLIER_ID"],
                exposure=option.excess_holding_cost,
                exposure_basis=(
                    f"the supplier's minimum forces {option.excess_qty} units more than needed, "
                    f"which cost {_inr(option.excess_holding_cost)} to hold while they sit"
                ),
                action_type=F.ACTION_RENEGOTIATE,
                action_detail=(
                    f"renegotiate {row.PART_ID} with {contract['SUPPLIER_ID']}: need "
                    f"{option.required_qty} but MOQ is {option.moq}, forcing "
                    f"{option.excess_months:.1f} months of overbuy"
                ),
                confidence=row.BURN_CONFIDENCE,
                suppression_key=f"{row.PART_ID}/{contract['SUPPLIER_ID']}",
                evidence=option.evidence,
                assumptions_used=F.assumption_values(
                    [F.ASSUMPTION_HOLDING_RATE, F.ASSUMPTION_SERVICE_LEVEL, F.ASSUMPTION_REVIEW_PERIOD],
                    criticality_class=row.CRITICALITY_CLASS,
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# All of them
# ---------------------------------------------------------------------------




def _leadtime_basis(
    annual_spend: float,
    cv: float,
    drift: float,
    erratic: bool,
    exposure: float,
    holding_rate: float,
) -> str:
    """The two terms of a supplier's cost, each priced, then the total.

    Either can be zero: a supplier who is erratic but punctual on average has no drift term, and
    printing "delivering 0.0 days late" describes a real state as though it were a bug.
    """
    # The buffer term counts ONLY when the supplier is erratic enough to cross the threshold --
    # `exposure` applies `cv if erratic else 0.0`. Computing it unconditionally here printed a
    # term the figure did not contain, so a supplier who is late but predictable showed a
    # Rs 20 lakh buffer cost that was in no total. The citation pass flagged that figure as
    # having no measurement behind it, which is exactly what it was.
    buffer_cost = annual_spend * holding_rate * cv if erratic else 0.0
    delay_cost = annual_spend * (max(drift, 0.0) / 365.0)

    terms = []
    if buffer_cost > 0:
        terms.append(
            f"{_inr(annual_spend)} a year x {holding_rate * 100:.0f}% to hold "
            f"x {cv:.2f} how erratic they are ≈ {_inr(buffer_cost)} of extra buffer"
        )
    if delay_cost > 0:
        terms.append(
            f"{_inr(annual_spend)} a year x {drift:.1f} days late / 365 "
            f"= {_inr(delay_cost)} of working capital tied up"
        )
    if not terms:
        return f"{_inr(exposure)} a year"
    return "; ".join(terms) + (f"; total {_inr(exposure)}" if len(terms) > 1 else "")

def _pct(value: float) -> str:
    """A probability as a percentage, at enough precision that the product ties out.

    A fixed `:.0f` printed the donor half as `rises 0% to 1%, costing Rs 45,76,893` -- there is no
    reading of a one-point move that reaches that figure -- and made the receiver half miss its
    own stated total: `57% x Rs 6,29,71,243` is Rs 3,58,93,608, not the Rs 3,55,86,690 printed
    beside it, because the real delta was 56.51%. A trail whose figures do not reproduce its total
    is worse than no trail, since it invites the reader to check it exactly once. Small
    probabilities need more decimals than large ones to carry the same relative accuracy, so the
    width scales with the value rather than being fixed.
    """
    pct = value * 100.0
    if pct <= 0:
        return "0%"
    if pct < 0.01:
        return "under 0.01%"
    if pct < 10:
        return f"{pct:.2f}%"
    return f"{pct:.1f}%"


def _transfer_basis(option, receiver: dict, donors_by_id: dict) -> str:
    """The transfer's arithmetic, ending on the figure the card shows.

    Both halves have to be visible, and visible means checkable: each side states its own
    `delta x consequence`, not just a rupee figure. The donor half used to print only its cost, so
    the Rs 45.77 lakh it charged against a transfer had no multiplicand anywhere on the page --
    the donor's consequence was in `evidence`, never in the sentence -- and a PM reading "rises 0%
    to 1%, costing Rs 45,76,893" cannot tell a measurement from a typo. "Moving a shortage is not
    a fix" is only a real constraint if the subtraction can be audited, not merely seen.
    """
    donor = donors_by_id.get(option.donor_warehouse_id, {})
    receiver_consequence = float(receiver["consequence"])
    donor_consequence = float(donor.get("consequence", 0.0))
    receiver_delta = option.receiver_risk_before - option.receiver_risk_after
    donor_delta = option.donor_risk_after - option.donor_risk_before
    receiver_gain = receiver_delta * receiver_consequence
    donor_cost = donor_delta * donor_consequence
    return (
        f"risk at {option.receiver_warehouse_id} falls "
        f"{_pct(option.receiver_risk_before)} to {_pct(option.receiver_risk_after)}, "
        f"so {_pct(receiver_delta)} x {_inr(receiver_consequence)} at stake there "
        f"≈ {_inr(receiver_gain)} saved; "
        f"risk at {option.donor_warehouse_id} rises "
        f"{_pct(option.donor_risk_before)} to {_pct(option.donor_risk_after)}, "
        f"so {_pct(donor_delta)} x {_inr(donor_consequence)} at stake there "
        f"≈ {_inr(donor_cost)} given up; net {_inr(receiver_gain - donor_cost)}"
    )

def scan_all(
    part_position: pd.DataFrame,
    parent_cascades: pd.DataFrame,
    supplier_performance: pd.DataFrame,
    settings: st.Settings | None = None,
) -> list[F.Finding]:
    """Every scanner, in one list. Ranking and suppression happen downstream.

    `settings` is resolved once by the job and handed down rather than read here, so the
    scanners stay pure functions the accuracy gates can run without a cluster.
    """
    cfg = settings or st.DEFAULTS
    return [
        *scan_stockout_risk(part_position, supplier_performance, cfg),
        *scan_cascade_block(parent_cascades, part_position, supplier_performance, cfg),
        *scan_redeployment(part_position, supplier_performance, cfg),
        *scan_dead_capital(part_position, cfg),
        *scan_leadtime_signal(supplier_performance, part_position, cfg),
        *scan_demand_shift(part_position, cfg),
        *scan_supplier_economics(part_position, supplier_performance, cfg),
        *scan_moq_uneconomic(part_position, supplier_performance, cfg),
    ]


def to_frame(found: list[F.Finding]) -> pd.DataFrame:
    return pd.DataFrame([f.to_row() for f in found])
