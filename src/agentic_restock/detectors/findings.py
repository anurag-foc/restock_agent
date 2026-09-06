"""The common finding shape, and the assumption-disclosure rule.

Eight scanners run at eight different grains — (part, warehouse), (parent, warehouse), (part)
across the network, (supplier), (supplier, part) — and all of them emit *this*. That is the
whole structural fix: flattening every nuance onto one (part, warehouse) row is how six of the
eight became columns that could never be the reason something was flagged.

Two fields carry the report's integrity:

**`evidence`** is populated by the scanner that found the thing, at detection time. This is what
removes the drill-down turns from the pipeline — not by optimising them, but by leaving nothing
for them to fetch. A self-describing finding cannot be narrated with a figure that was never
measured, cannot omit a check, and cannot report "no data" about a call it never made.

**`assumptions_used`** records only the policy inputs *this* finding's numbers depend on. A
transfer spends nothing and holds nothing, so it discloses no holding rate. This is structural
rather than a prompt instruction: the narration step can only list what the scanner recorded, so
the "optional slot gets filled anyway" failure — which survived two prose fixes and fabricated a
holding cost against a real zero — has nothing to fill from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agentic_restock.generation import policy

# --- finding types, one per scanner ----------------------------------------

STOCKOUT_RISK = "STOCKOUT_RISK"
CASCADE_BLOCK = "CASCADE_BLOCK"
REDEPLOYMENT = "REDEPLOYMENT"
DEAD_CAPITAL = "DEAD_CAPITAL"
LEADTIME_SIGNAL = "LEADTIME_SIGNAL"
DEMAND_SHIFT = "DEMAND_SHIFT"
SUPPLIER_ECONOMICS = "SUPPLIER_ECONOMICS"
MOQ_UNECONOMIC = "MOQ_UNECONOMIC"

ALL_FINDING_TYPES = (
    STOCKOUT_RISK,
    CASCADE_BLOCK,
    REDEPLOYMENT,
    DEAD_CAPITAL,
    LEADTIME_SIGNAL,
    DEMAND_SHIFT,
    SUPPLIER_ECONOMICS,
    MOQ_UNECONOMIC,
)

# --- subject grains --------------------------------------------------------

SUBJECT_PART_WAREHOUSE = "PART_WAREHOUSE"
SUBJECT_PARENT_WAREHOUSE = "PARENT_WAREHOUSE"
SUBJECT_PART_NETWORK = "PART_NETWORK"
SUBJECT_SUPPLIER = "SUPPLIER"
SUBJECT_SUPPLIER_PART = "SUPPLIER_PART"

# --- action types ----------------------------------------------------------

ACTION_TRANSFER = "TRANSFER"
ACTION_PURCHASE = "PURCHASE"
ACTION_RENEGOTIATE = "RENEGOTIATE"
ACTION_RESOURCE = "RESOURCE"
ACTION_RECALIBRATE = "RECALIBRATE"
ACTION_REVIEW_STOCK = "REVIEW_STOCK"
ACTION_NONE = "NONE"

# --- assumption keys -------------------------------------------------------

ASSUMPTION_HOLDING_RATE = "holding_rate"
ASSUMPTION_SERVICE_LEVEL = "service_level"
ASSUMPTION_REVIEW_PERIOD = "review_period_days"
ASSUMPTION_CASCADE_HORIZON = "cascade_horizon_days"
ASSUMPTION_CONSEQUENCE_PROXY = "consequence_criticality_proxy"


def assumption_values(keys: list[str], *, criticality_class: str | None = None) -> dict:
    """Resolve assumption keys to the values in force, labelled as policy settings.

    Labelled deliberately. A PM reading `14%` needs to know a person chose it rather than it
    being derived from their data — that distinction is the difference between a number they
    will argue with and one they will assume is fixed.
    """
    resolved: dict[str, dict] = {}
    for key in keys:
        if key == ASSUMPTION_HOLDING_RATE:
            resolved[key] = {
                "value": policy.HOLDING_RATE,
                "display": f"{policy.HOLDING_RATE:.0%} per year",
                "kind": "policy",
                "basis": "bottom-up: "
                + ", ".join(f"{k} {v:.1%}" for k, v in policy.HOLDING_RATE_COMPONENTS.items()),
            }
        elif key == ASSUMPTION_SERVICE_LEVEL:
            tier = policy.normalise_criticality(criticality_class)
            resolved[key] = {
                "value": policy.SERVICE_LEVEL_BY_CLASS[tier],
                "display": f"{policy.SERVICE_LEVEL_BY_CLASS[tier]:.0%} ({tier})",
                "kind": "policy",
                "basis": "tiered by criticality class",
            }
        elif key == ASSUMPTION_REVIEW_PERIOD:
            resolved[key] = {
                "value": policy.REVIEW_PERIOD_DAYS,
                "display": f"{policy.REVIEW_PERIOD_DAYS} days",
                "kind": "policy",
                "basis": "weekly purchase-order cycle",
            }
        elif key == ASSUMPTION_CASCADE_HORIZON:
            from agentic_restock.jobs.positions import CASCADE_HORIZON_DAYS

            resolved[key] = {
                "value": CASCADE_HORIZON_DAYS,
                "display": f"{CASCADE_HORIZON_DAYS} days",
                "kind": "policy",
                "basis": "MRP frozen window",
            }
        elif key == ASSUMPTION_CONSEQUENCE_PROXY:
            resolved[key] = {
                "value": None,
                "display": "estimated, not measured",
                "kind": "approximation",
                "basis": "no production plan behind this part; consequence is a "
                "criticality-weighted multiple of stock value",
            }
    return resolved


@dataclass
class Finding:
    """One thing worth a PM's attention, from whichever scanner found it."""

    finding_type: str
    subject_type: str
    subject_id: str
    part_id: str | None = None
    warehouse_id: str | None = None
    supplier_id: str | None = None

    # --- the money -----------------------------------------------------
    exposure: float = 0.0
    p_stockout: float | None = None
    consequence: float | None = None
    action_type: str = ACTION_NONE
    action_detail: str = ""
    action_cost: float = 0.0
    # How `exposure` was arrived at, in words and figures, built where the inputs are in hand.
    #
    # There is no single formula: a stockout risk is P(stockout) x consequence, a transfer is net
    # risk removed, dead capital is a carrying cost, a cascade is blocked production value. A PM
    # asked to defend the number cannot, unless the derivation travels with it -- and letting the
    # model reconstruct it later is exactly the shape of every fabrication on record, since the
    # inputs are plausible and available but the arithmetic is not.
    exposure_basis: str = ""


    # --- integrity -----------------------------------------------------
    evidence: dict = field(default_factory=dict)
    assumptions_used: dict = field(default_factory=dict)
    confidence: str = "MEDIUM"
    suppression_key: str = ""

    @property
    def decision_value(self) -> float:
        """`exposure - action_cost`, floored at zero.

        `action_cost` is a real rupee figure from the fix constructors, not a percentage of
        exposure. That change is what removes the fabrication-bait the model kept reaching for.
        """
        return max(self.exposure - self.action_cost, 0.0)

    def to_row(self) -> dict:
        return {
            "FINDING_TYPE": self.finding_type,
            "SUBJECT_TYPE": self.subject_type,
            "SUBJECT_ID": self.subject_id,
            "PART_ID": self.part_id,
            "WAREHOUSE_ID": self.warehouse_id,
            "SUPPLIER_ID": self.supplier_id,
            "EXPOSURE": self.exposure,
            "P_STOCKOUT": self.p_stockout,
            "CONSEQUENCE": self.consequence,
            "ACTION_TYPE": self.action_type,
            "ACTION_DETAIL": self.action_detail,
            "ACTION_COST": self.action_cost,
            "DECISION_VALUE": self.decision_value,
            "EVIDENCE_JSON": json.dumps(self.evidence, sort_keys=True, default=str),
            "ASSUMPTIONS_JSON": json.dumps(self.assumptions_used, sort_keys=True, default=str),
            "CONFIDENCE": self.confidence,
            "SUPPRESSION_KEY": self.suppression_key,
        }
