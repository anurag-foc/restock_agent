"""The finding catalog, as data.

docs/dataset_generator_spec.md §1 declares eleven findings the system must produce. This module
is that catalog in executable form: which parts, warehouses and suppliers carry which planted
finding, and under what condition. Every other generator reads from here rather than choosing
its own numbers, so the dataset's intent stays in one readable place instead of being an
emergent property of parameters scattered across modules.

Ranges are reserved so findings cannot silently collide:

    P0001-P0012   assemblies      (level 0)  -- cascade parents
    P0013-P0040   sub-assemblies  (level 1)
    P0041-P0069   focus components, scenario-bearing
    P0070-P0080   focus components, F11 + spare capacity
    P0081-P0090   dead capital
    P0091-P0100   intermittent spares

The one deliberate collision is F3: a part that is simultaneously below its own safety stock and
binding a parent's build. That is the point of F3 -- the current implementation excludes exactly
that case from its cascade join and prices the worst problem in the system at the cost of the
parts.
"""

from __future__ import annotations

from functools import cache

from agentic_restock.generation import bom
from agentic_restock.generation.entities import (
    ACTIVE_WAREHOUSE_IDS,
    PLANT_STORE_IDS,
    parts,
    supplier_with_archetype,
)

# Regional distribution centres -- everything active that is not a plant store.
RDC_IDS = [w for w in ACTIVE_WAREHOUSE_IDS if w not in PLANT_STORE_IDS]

TODAY = "2026-09-04"
HISTORY_START = "2023-09-01"


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

FINDINGS: dict[str, dict] = {
    "F1": {
        "name": "Transfer beats buying",
        "proves": "nuance 1; donor ranking is non-trivial, not 'find any surplus'",
        "part": "P0050",
        # WH006 holds the largest absolute surplus but burns fast, so its own cover is thin.
        # WH004 is the correct donor. A detector that ranks donors by quantity picks wrong.
        "receiver": "WH003",
        "best_donor": "WH004",
        "decoy_donor": "WH006",
        "neutral": ["WH005", "WH007"],
    },
    "F2": {
        "name": "Cascade blocked by two co-binding components",
        "proves": "cascade computed at the parent; 'fix one child' is false",
        "parent_assembly": "P0001",
        "binding_children": ["P0041", "P0069"],
        "warehouse": "WH001",
    },
    "F3": {
        "name": "The underpriced worst case",
        "proves": "a part both short and cascade-binding gets the production consequence, "
        "not the cost of the parts",
        "part": "P0042",
        "parent_assembly": "P0002",
        "warehouse": "WH001",
    },
    "F4": {
        "name": "Variance, not drift",
        "proves": "lead time measures spread, not just mean; zero drift can still be unmanageable",
        # Resolved by ARCHETYPE, never by id: a hard-coded id points at whatever archetype
        # happens to sit at that position, and rebalancing the mix once turned this into a
        # `tight` supplier -- inverting the finding with no error raised.
        "erratic_supplier": supplier_with_archetype("loose"),
        "drifting_supplier": supplier_with_archetype("drifting"),
        "parts": ["P0051", "P0052"],
    },
    "F5": {
        "name": "The cheap supplier is expensive",
        "proves": "effective_unit_cost reverses the quoted-price ranking",
        "part": "P0055",
        # (supplier, relative price) -- cheapest quote, worst reject rate and spread.
        #
        # The spread is deliberately SMALL. At 1.00/1.12/1.25 the cheap supplier's 6% reject
        # rate and wide lead-time spread came nowhere near closing a 25% price gap, so it was
        # genuinely the cheapest on effective cost too and the reversal could not happen. Real
        # competing quotes on one part differ by a few percent, which is exactly the regime where
        # quality and reliability decide it.
        "suppliers": [
            (supplier_with_archetype("cheap_and_bad"), 1.00),
            (supplier_with_archetype("drifting", 1), 1.03),
            (supplier_with_archetype("tight"), 1.045),
        ],
    },
    "F6": {
        "name": "MOQ makes the fix uneconomic",
        "proves": "nuance 7 producing a different action type -- renegotiate, not order",
        "part": "P0060",
        "supplier": supplier_with_archetype("tight", 1),
        "moq": 500,
        "pack_size": 1,
        "target_daily_burn": 2.0,  # 440 excess -> 220 days -> ~7.3 months of overbuy
    },
    "F7": {
        "name": "Demand shifted, safety stock did not",
        "proves": "nuance 4 as a finding; needs sustained history to distinguish from one spike",
        "part": "P0045",
        "warehouse": "WH005",
        "step_factor": 1.6,
        "step_days_ago": 120,
    },
    "F8": {
        "name": "Dead capital",
        "proves": "the excess half of the network view; 38% of inventory is excess (§2)",
        "pairs": [
            ("P0081", "WH004"),
            ("P0082", "WH005"),
            ("P0083", "WH006"),
            ("P0084", "WH007"),
            ("P0085", "WH008"),
            ("P0086", "WH003"),
        ],
        "quiet_days": 200,
        "cover_days": 400,
    },
    "F9": {
        "name": "Intermittent part, no false shortage",
        # CORRECTED. The original claim was "the detector must not fire at all", which is wrong:
        # a spare burning 0.33/day while holding 808 units has 2,431 days of cover, and reporting
        # that as dead capital is correct and valuable. What must NOT happen is mistaking lumpy
        # demand for an imminent shortage -- a daily mean over 92% zero days would put cover at a
        # few days and raise a stockout that is not there.
        "proves": "a negative test -- lumpy demand must not be read as an imminent shortage",
        "silent_scanners": ["STOCKOUT_RISK", "DEMAND_SHIFT"],
        "parts": ["P0091", "P0092", "P0093", "P0094", "P0095", "P0096", "P0099", "P0100"],
        "warehouse": "WH003",
        "mean_interval_days": 12,
        "mean_issue_size": 4,
    },
    "F10": {
        "name": "Quiet majority",
        "proves": "the alert-fatigue thesis; quiet must be the normal case",
        "min_quiet_fraction": 0.65,
    },
    "F11": {
        "name": "Ranking inversion",
        "proves": "closes §16 -- does decision value order differently from raw exposure",
        # The inversion is constructed on the COST side, not by targeting exposure. Exposure is
        # several steps downstream of stock -- p_stockout x consequence, where consequence is a
        # criticality multiple of unserved demand -- so it cannot be dialled to a figure. Earlier
        # `*_target` rupee values were declared here and never read by anything, which left the
        # whole inversion uncontrolled.
        #
        # Instead: the bigger exposure gets a fix that is expensive by construction (a 60-day
        # lead and a high minimum order), so `exposure - action_cost` falls below a smaller
        # exposure whose fix is nearly free.
        "high_exposure_part": "P0071",
        "high_exposure_warehouse": "WH006",
        "high_exposure_lead_days": 60,
        "high_exposure_moq": 400,
        "high_exposure_supplier": supplier_with_archetype("tight", 2),
        # Smaller exposure, but a transfer fixes it for freight -- should rank FIRST on decision
        # value while ranking SECOND on raw exposure.
        "transfer_fixable_part": "P0070",
        "transfer_fixable_warehouse": "WH005",
        "transfer_fixable_donor": "WH004",
    },
}


# ---------------------------------------------------------------------------
# Lead-time history cohorts (spec §1, "Lead-time fallback tiers")
# ---------------------------------------------------------------------------
#
# E2 falls back (supplier,part) -> (supplier) -> (category) -> contracted. All four tiers must be
# reachable or the hierarchy is untestable, so delivery history is deliberately uneven.

DELIVERY_HISTORY_TIERS = {
    "rich": {"pairs": 8, "deliveries": 12, "reaches": "(supplier, part)"},
    "medium": {"pairs": 10, "deliveries": 4, "reaches": "(supplier)"},
    "sparse": {"pairs": 12, "deliveries": 2, "reaches": "(supplier category)"},
    "none": {"pairs": 10, "deliveries": 0, "reaches": "contracted only"},
}


# ---------------------------------------------------------------------------
# History depth cohorts (spec §3.1, "History depth")
# ---------------------------------------------------------------------------
#
# The seasonal-index shrinkage term is min(years/3, 1), which is exactly 1.0 at 36 months. With
# every pair on full history, neither shrinkage nor the LOW-confidence path ever executes.

HISTORY_DEPTH_COHORTS = {
    "full": {"days": 1100, "exercises": "seasonal index at full weight"},
    "shrunk": {"days": 400, "exercises": "shrinkage active (< 2 years)"},
    "sparse": {"days": 60, "exercises": "no seasonality, burn_confidence = LOW"},
}

SHRUNK_HISTORY_PAIRS = 10
SPARSE_HISTORY_PAIRS = 5


# ---------------------------------------------------------------------------
# The (part, warehouse) universe
# ---------------------------------------------------------------------------


def _warehouses_for(part: dict, index: int) -> list[str]:
    """Which warehouses stock this part.

    Assemblies and sub-assemblies live only at the plant stores -- they are consumed by
    production, not distributed. Focus components live at both plant stores (so the cascade has
    component availability to check) *and* several RDCs (so lateral transfer has a real network
    to search). Background components sit at one or two RDCs.
    """
    part_id = part["PART_ID"]
    level = part["BOM_LEVEL"]

    if level in (0, 1):
        return list(PLANT_STORE_IDS)

    if part_id in _dead_capital_parts():
        return [dict(FINDINGS["F8"]["pairs"])[part_id]]

    if part_id in FINDINGS["F9"]["parts"]:
        return [FINDINGS["F9"]["warehouse"]]

    if part_id == FINDINGS["F1"]["part"]:
        f1 = FINDINGS["F1"]
        return [f1["receiver"], f1["best_donor"], f1["decoy_donor"], *f1["neutral"]]

    f11 = FINDINGS["F11"]
    if part_id == f11["transfer_fixable_part"]:
        # Needs a donor with real surplus, so a transfer is genuinely the cheapest fix.
        return list(PLANT_STORE_IDS) + [
            f11["transfer_fixable_warehouse"],
            f11["transfer_fixable_donor"],
        ]
    if part_id == f11["high_exposure_part"]:
        # Deliberately NO network peer with surplus -- the only fix is a long-lead buy, which is
        # what makes its decision value fall below the smaller, transfer-fixable exposure.
        return list(PLANT_STORE_IDS) + [f11["high_exposure_warehouse"]]

    # Remaining focus components: both plant stores plus a rotating pair of RDCs, so every part
    # has a network but no single RDC carries everything.
    rotating = [RDC_IDS[index % len(RDC_IDS)], RDC_IDS[(index + 2) % len(RDC_IDS)]]
    return list(PLANT_STORE_IDS) + rotating


@cache
def _dead_capital_parts() -> set[str]:
    return {p for p, _ in FINDINGS["F8"]["pairs"]}


@cache
def pair_universe() -> list[dict]:
    """Every (part, warehouse) the dataset stocks, with the finding ids each pair carries."""
    rows: list[dict] = []
    for index, part in enumerate(parts()):
        for warehouse_id in _warehouses_for(part, index):
            rows.append(
                {
                    "part_id": part["PART_ID"],
                    "warehouse_id": warehouse_id,
                    "bom_level": part["BOM_LEVEL"],
                    "unit_cost": part["UNIT_COST"],
                    "criticality_class": part["CRITICALITY_CLASS"],
                    "finding_ids": findings_for(part["PART_ID"], warehouse_id),
                }
            )
    return rows


@cache
def findings_for(part_id: str, warehouse_id: str) -> list[str]:
    """Finding ids planted on this pair. Empty means the pair should stay quiet (F10)."""
    hits: list[str] = []
    f = FINDINGS

    if part_id == f["F1"]["part"]:
        hits.append("F1")
    if part_id in f["F2"]["binding_children"] and warehouse_id == f["F2"]["warehouse"]:
        hits.append("F2")
    if part_id == f["F3"]["part"] and warehouse_id == f["F3"]["warehouse"]:
        hits.append("F3")
    if part_id in f["F4"]["parts"]:
        hits.append("F4")
    if part_id == f["F5"]["part"]:
        hits.append("F5")
    if part_id == f["F6"]["part"]:
        hits.append("F6")
    if part_id == f["F7"]["part"] and warehouse_id == f["F7"]["warehouse"]:
        hits.append("F7")
    if (part_id, warehouse_id) in f["F8"]["pairs"]:
        hits.append("F8")
    if part_id in f["F9"]["parts"] and warehouse_id == f["F9"]["warehouse"]:
        hits.append("F9")
    if part_id == f["F11"]["high_exposure_part"] and warehouse_id == f["F11"]["high_exposure_warehouse"]:
        hits.append("F11")
    if (
        part_id == f["F11"]["transfer_fixable_part"]
        and warehouse_id == f["F11"]["transfer_fixable_warehouse"]
    ):
        hits.append("F11")

    return hits


@cache
def expect_finding(part_id: str, warehouse_id: str) -> bool:
    """False for pairs that must stay quiet — the denominator for detector precision.

    F9 is deliberately excluded: intermittent spares carry a finding id because the dataset
    plants a *condition* there, but no *shortage* is expected. They may legitimately produce a
    dead-capital finding -- see the corrected note on F9 -- so this flag means "expect a shortage
    finding", and the F9 assertion checks the shortage scanners specifically.
    """
    hits = findings_for(part_id, warehouse_id)
    return bool([h for h in hits if h != "F9"])


@cache
def scenario_parts() -> set[str]:
    """Every part named anywhere in the catalog — useful for asserting no accidental reuse."""
    named: set[str] = set()
    for finding in FINDINGS.values():
        for key, value in finding.items():
            if key.endswith("pairs"):
                named.update(p for p, _ in value)
            elif isinstance(value, str) and value.startswith("P0"):
                named.add(value)
            elif isinstance(value, list) and value and isinstance(value[0], str):
                named.update(v for v in value if v.startswith("P0"))
    return named


@cache
def required_pairs() -> list[tuple[str, str, str]]:
    """Every (part, warehouse) the catalog requires to be stocked, tagged by finding and role.

    Enumerated explicitly rather than inferred from key naming: an earlier version validated
    only F1's roles and F8's `pairs`, and F11's two warehouses were silently absent from the
    stocked universe — so the ranking-inversion finding could never fire, with no error anywhere.
    Anything the catalog depends on has to be listed here to be checked.
    """
    f = FINDINGS
    pairs: list[tuple[str, str, str]] = []

    f1 = f["F1"]
    for role in ("receiver", "best_donor", "decoy_donor"):
        pairs.append((f1["part"], f1[role], f"F1.{role}"))
    for wh in f1["neutral"]:
        pairs.append((f1["part"], wh, "F1.neutral"))

    for child in f["F2"]["binding_children"]:
        pairs.append((child, f["F2"]["warehouse"], "F2.binding_child"))

    pairs.append((f["F3"]["part"], f["F3"]["warehouse"], "F3.short_and_binding"))
    pairs.append((f["F7"]["part"], f["F7"]["warehouse"], "F7.step_change"))

    for part_id, wh in f["F8"]["pairs"]:
        pairs.append((part_id, wh, "F8.dead_capital"))

    for part_id in f["F9"]["parts"]:
        pairs.append((part_id, f["F9"]["warehouse"], "F9.intermittent"))

    f11 = f["F11"]
    pairs.append((f11["high_exposure_part"], f11["high_exposure_warehouse"], "F11.buy_only"))
    pairs.append(
        (f11["transfer_fixable_part"], f11["transfer_fixable_warehouse"], "F11.transfer_fixable")
    )
    pairs.append((f11["transfer_fixable_part"], f11["transfer_fixable_donor"], "F11.donor"))

    return pairs


@cache
def cascade_parents() -> dict[str, list[str]]:
    """Assembly -> the components the catalog plants as binding, for the cascade findings."""
    return {
        FINDINGS["F2"]["parent_assembly"]: list(FINDINGS["F2"]["binding_children"]),
        FINDINGS["F3"]["parent_assembly"]: [FINDINGS["F3"]["part"]],
    }


def validate_catalog() -> list[str]:
    """Structural checks on the catalog itself. Returns problems; empty means consistent.

    Runs before any data is generated, because a catalog that references a part outside its
    reserved range or a warehouse that is not ACTIVE produces a dataset whose findings quietly
    cannot fire -- which is the exact failure the spec's finding-first principle exists to avoid.
    """
    problems: list[str] = []
    valid_parts = {p["PART_ID"] for p in parts()}
    universe = {(r["part_id"], r["warehouse_id"]) for r in pair_universe()}

    for part_id in scenario_parts():
        if part_id not in valid_parts:
            problems.append(f"catalog references unknown part {part_id}")

    # Every cascade-binding child must actually descend from its declared parent, or the
    # explosion will never reach it.
    for parent, children in cascade_parents().items():
        descendants = set(bom.descendants_of(parent))
        for child in children:
            if child not in descendants:
                problems.append(f"{child} is not a descendant of {parent}")

    # Every pair the catalog depends on must exist in the stocked universe.
    for part_id, warehouse_id, role in required_pairs():
        if (part_id, warehouse_id) not in universe:
            problems.append(f"{role} needs ({part_id}, {warehouse_id}) which is not stocked")

    # Every finding must be reachable from at least one pair, or it silently cannot fire.
    planted = {f for r in pair_universe() for f in r["finding_ids"]}
    for finding_id in FINDINGS:
        if finding_id == "F10":  # a property of the whole set, not of any one pair
            continue
        if finding_id not in planted:
            problems.append(f"{finding_id} is declared but planted on no pair")

    if not expect_finding(FINDINGS["F3"]["part"], FINDINGS["F3"]["warehouse"]):
        problems.append("F3's pair is not marked as expecting a finding")

    for part_id in FINDINGS["F9"]["parts"]:
        if expect_finding(part_id, FINDINGS["F9"]["warehouse"]):
            problems.append(f"F9 part {part_id} must not expect a finding — silence is correct")

    return problems
