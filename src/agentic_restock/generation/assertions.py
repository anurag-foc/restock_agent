"""Generator self-assertions — docs/dataset_generator_spec.md §5.

These run before anything is loaded and fail loudly. A subtly wrong dataset is worse than no
dataset: every detector built on top of it would be graded against the wrong answers, and the
Phase 2 gate would report a number that looked plausible and meant nothing.

The most important one is §5.2. A finding catalog that silently stops firing — because a
scenario's warehouse changed, or an archetype was rebalanced, or a target got steered off — is
the failure mode this whole spec exists to prevent, and it has already happened twice during the
build (F11 was unplantable; F2's children stopped co-binding).
"""

from __future__ import annotations

import hashlib
import itertools
import json

import numpy as np

from agentic_restock.generation import (
    bom,
    contracts,
    dataset,
    demand,
    entities,
    ground_truth,
    production,
    replenishment,
    scenarios,
    snapshots,
    supplier_facts,
)

MIN_MEMBERS_PER_COHORT = 3
MIN_QUIET_FRACTION = 0.65


def check_reconciliation(histories) -> list[str]:
    """§5.1 — on_hand[t] == on_hand[t-1] - issues[t] + receipts[t], and never negative."""
    problems: list[str] = []
    for (part_id, warehouse_id), history in histories.items():
        expected = (
            history.opening_qty - np.cumsum(history.issues) + np.cumsum(history.receipts)
        ).astype(int)
        if not np.array_equal(history.on_hand, expected):
            problems.append(f"reconciliation broken for {part_id}@{warehouse_id}")
        if history.on_hand.min() < 0:
            problems.append(
                f"{part_id}@{warehouse_id} holds negative stock ({int(history.on_hand.min())})"
            )
    return problems


def check_snapshot_ties_to_transactions(data: dict[str, list[dict]]) -> list[str]:
    """§5.1, from the loaded rows' side — the audit property a reader would actually check.

    Snapshots are sampled, so this verifies the balance change between two *sampled* dates equals
    the net transactions between them. If sampling ever broke that, the dataset would stop being
    auditable without anything else failing.
    """
    snapshots_by_pair: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for row in data["fact_inventory_snapshot"]:
        key = (row["PART_KEY"], row["WAREHOUSE_KEY"])
        snapshots_by_pair.setdefault(key, []).append(
            (row["SNAPSHOT_DATE_KEY"], row["QUANTITY_ON_HAND"])
        )

    net_by_pair_date: dict[tuple[int, int], dict[int, int]] = {}
    for row in data["fact_inventory_transaction"]:
        key = (row["PART_KEY"], row["WAREHOUSE_KEY"])
        sign = 1 if row["TRANSACTION_TYPE"] == snapshots.TXN_RECEIPT else -1
        bucket = net_by_pair_date.setdefault(key, {})
        date_key = row["TRANSACTION_DATE_KEY"]
        bucket[date_key] = bucket.get(date_key, 0) + sign * row["QUANTITY"]

    problems: list[str] = []
    for key, series in snapshots_by_pair.items():
        series.sort()
        movements = net_by_pair_date.get(key, {})
        for (date_a, qty_a), (date_b, qty_b) in itertools.pairwise(series):
            net = sum(
                delta for date, delta in movements.items() if date_a < date <= date_b
            )
            if qty_b - qty_a != net:
                problems.append(
                    f"{key}: snapshot moved {qty_b - qty_a} between {date_a} and {date_b} "
                    f"but transactions net {net}"
                )
                break  # one report per pair is enough
    return problems


def check_every_finding_is_live(histories) -> list[str]:
    """§5.2 — each planted finding must actually be detectable on today's data."""
    problems: list[str] = []
    f = scenarios.FINDINGS

    planted = {
        finding
        for history in histories.values()
        for finding in history.params.finding_ids
    }
    for finding_id in set(f) - {"F10"}:
        if finding_id not in planted:
            problems.append(f"{finding_id} is declared but planted on no pair")

    # F1: a short receiver, a real donor, and a decoy holding more units but less cover.
    f1 = f["F1"]
    receiver = histories[(f1["part"], f1["receiver"])]
    best = histories[(f1["part"], f1["best_donor"])]
    decoy = histories[(f1["part"], f1["decoy_donor"])]
    if receiver.closing_qty >= receiver.params.safety_stock:
        problems.append("F1 receiver is not short")
    if decoy.closing_qty <= best.closing_qty:
        problems.append("F1 decoy does not hold more units than the real donor")
    if decoy.closing_qty / decoy.params.effective_level >= (
        best.closing_qty / best.params.effective_level
    ):
        problems.append("F1 decoy does not have thinner cover than the real donor")

    # F2/F3: the cascade must actually bind, and F2's two children must bind together.
    requirements = production.part_requirements()
    for finding_id in ("F2", "F3"):
        spec = f[finding_id]
        parent = spec["parent_assembly"]
        warehouse = spec["warehouse"]
        levels: list[tuple[int, str]] = []
        for child in bom.descendants_of(parent):
            history = histories.get((child, warehouse))
            if history is None:
                continue
            multiplier = bom.multiplier_to_ancestor(child, parent) or 1
            available = history.closing_qty + history.in_transit_qty
            levels.append((available // multiplier, child))
        if not levels:
            problems.append(f"{finding_id}: no children of {parent} stocked at {warehouse}")
            continue
        levels.sort()
        buildable = levels[0][0]
        if requirements.get(parent, 0) <= buildable:
            problems.append(
                f"{finding_id}: {parent} is not blocked "
                f"(needs {requirements.get(parent, 0):.0f}, can build {buildable})"
            )
        binding = {child for level, child in levels if level == buildable}
        expected = set(spec.get("binding_children") or [spec["part"]])
        if binding != expected:
            problems.append(
                f"{finding_id}: binding set is {sorted(binding)}, expected {sorted(expected)}"
            )

    # F6: the replenishment gap must be small enough that MOQ dwarfs it.
    f6 = f["F6"]
    history = next(h for k, h in histories.items() if k[0] == f6["part"])
    gap = history.params.max_stock - history.closing_qty
    contract = contracts.contract_for(f6["part"], f6["supplier"])
    if contract is None:
        problems.append("F6 has no contract")
    elif contract["moq"] < gap * 4:
        problems.append(f"F6 MOQ {contract['moq']} does not dwarf a gap of {gap}")

    # F9: intermittent spares must be lumpy enough to route to Croston, not merely noisy.
    for part_id in f["F9"]["parts"]:
        history = histories[(part_id, f["F9"]["warehouse"])]
        zero_fraction = float((history.issues == 0).mean())
        if zero_fraction <= 0.7:
            problems.append(
                f"F9 {part_id} is only {zero_fraction:.0%} zero days; needs >70%"
            )

    # F11: the inversion needs a transfer-fixable side WITH a donor and a buy-only side WITHOUT.
    f11 = f["F11"]
    donor = histories[(f11["transfer_fixable_part"], f11["transfer_fixable_donor"])]
    if donor.closing_qty <= donor.params.safety_stock:
        problems.append("F11 transfer-fixable side has no donor with surplus")
    for (part_id, warehouse_id), history in histories.items():
        if part_id != f11["high_exposure_part"]:
            continue
        if warehouse_id == f11["high_exposure_warehouse"]:
            continue
        if history.closing_qty > history.params.safety_stock:
            problems.append(
                f"F11 buy-only side has a donor at {warehouse_id} "
                f"({history.closing_qty - history.params.safety_stock} spare)"
            )
    return problems


def check_quiet_majority(histories) -> list[str]:
    """§5.3 — quiet has to be the normal case or the alert-fatigue claim is unprovable."""
    expecting = sum(1 for key in histories if scenarios.expect_finding(*key))
    quiet_fraction = 1 - expecting / len(histories)
    if quiet_fraction < MIN_QUIET_FRACTION:
        return [
            (
                f"only {quiet_fraction:.0%} of pairs are quiet; "
                f"at least {MIN_QUIET_FRACTION:.0%} required"
            )
        ]
    return []


def check_cohort_coverage(histories) -> list[str]:
    """§5.4 — no detector branch may be dead code for want of examples."""
    problems: list[str] = []

    regimes: dict[str, int] = {}
    cohorts: dict[str, int] = {}
    for history in histories.values():
        regimes[history.params.regime] = regimes.get(history.params.regime, 0) + 1
        cohorts[history.params.history_cohort] = (
            cohorts.get(history.params.history_cohort, 0) + 1
        )

    for regime in demand._NOISE_CV:
        if regimes.get(regime, 0) < MIN_MEMBERS_PER_COHORT:
            problems.append(f"demand regime '{regime}' has {regimes.get(regime, 0)} members")

    for cohort in scenarios.HISTORY_DEPTH_COHORTS:
        if cohorts.get(cohort, 0) < 1:
            problems.append(f"history cohort '{cohort}' is empty")

    archetypes: dict[str, int] = {}
    for supplier in entities.suppliers():
        archetypes[supplier["_archetype"]] = archetypes.get(supplier["_archetype"], 0) + 1
    for archetype in entities.SUPPLIER_ARCHETYPES:
        if archetypes.get(archetype, 0) < MIN_MEMBERS_PER_COHORT:
            problems.append(
                f"supplier archetype '{archetype}' has {archetypes.get(archetype, 0)} members"
            )

    return problems


#: Tiers of E2's ladder that this dataset must reach. `contracted` is excluded deliberately:
#: with 744 captured deliveries there is always *some* pool to estimate from, and using it is the
#: correct behaviour -- falling through to the contract while data exists would be worse. It is a
#: genuine cold-start path, unit-tested directly in tests/test_ground_truth_recovery.py, and
#: forcing the dataset to produce it would mean degrading the dataset to suit an assertion.
DATA_BACKED_FALLBACK_TIERS = ("supplier_part", "supplier", "supplier_category")

MIN_PAIRS_PER_FALLBACK_TIER = 3


def check_fallback_tiers(records) -> list[str]:
    """§5.5 — every data-backed lead-time fallback tier must be reachable."""
    rows = ground_truth.supplier_part_rows(records)
    tiers: dict[str, int] = {}
    for row in rows:
        tier = row["EXPECTED_FALLBACK_TIER"]
        tiers[tier] = tiers.get(tier, 0) + 1
    return [
        f"lead-time fallback tier '{tier}' has {tiers.get(tier, 0)} pairs, "
        f"needs {MIN_PAIRS_PER_FALLBACK_TIER}"
        for tier in DATA_BACKED_FALLBACK_TIERS
        if tiers.get(tier, 0) < MIN_PAIRS_PER_FALLBACK_TIER
    ]


def check_referential_integrity(data: dict[str, list[dict]]) -> list[str]:
    """§5.6 — no orphan surrogate keys, and no `-1` sentinel dates left anywhere."""
    problems: list[str] = []

    valid = {
        "PART_KEY": {p["PART_KEY"] for p in data["dim_part"]},
        "WAREHOUSE_KEY": {w["WAREHOUSE_KEY"] for w in data["dim_warehouse"]},
        "SUPPLIER_KEY": {s["SUPPLIER_KEY"] for s in data["dim_supplier"]},
        "PLANT_KEY": {p["PLANT_KEY"] for p in data["dim_plant"]},
        "LINE_KEY": {line["LINE_KEY"] for line in data["dim_production_line"]},
        "MODEL_KEY": {m["MODEL_KEY"] for m in data["dim_vehicle_model"]},
    }

    for table, rows in data.items():
        if table.startswith("dim_") or table == "sim_ground_truth":
            continue
        for row in rows:
            for column, allowed in valid.items():
                value = row.get(column)
                if value is None or value == -1:  # -1 is the documented "unknown" member
                    continue
                if value not in allowed:
                    problems.append(f"{table}.{column}={value} references no dimension row")
                    break
            for column, value in row.items():
                if column.endswith("_DATE_KEY") and value is not None and value <= 0:
                    problems.append(f"{table}.{column} still carries a sentinel ({value})")
                    break

    return problems


def check_ground_truth_is_gradeable(data: dict[str, list[dict]]) -> list[str]:
    """Every column Phase 2's gate grades against must actually carry values.

    Added after a silent failure: the staged schema was taken from the first row, so on a table
    holding two row shapes every supplier-only column was dropped and then padded with NULL. The
    load reported success, `sim_ground_truth` had all 340 rows, and the half of the gate that
    grades the lead-time estimator was blank. Row counts cannot detect that; only checking the
    columns can.
    """
    problems: list[str] = []
    rows = data.get("sim_ground_truth", [])
    if not rows:
        return ["sim_ground_truth is empty"]

    required = {
        "PAIR": ["TRUE_EFFECTIVE_LEVEL", "TRUE_LEVEL", "REALISED_ISSUE_MEAN", "REGIME"],
        "SUPPLIER_PART": [
            "TRUE_MU_LEAD_DAYS",
            "TRUE_SIGMA_LEAD_DAYS",
            "CAPTURE_TIER",
            "EXPECTED_FALLBACK_TIER",
            "OBSERVED_DELIVERY_N",
        ],
    }

    for subject_type, columns in required.items():
        subject_rows = [r for r in rows if r.get("SUBJECT_TYPE") == subject_type]
        if not subject_rows:
            problems.append(f"no {subject_type} rows in sim_ground_truth")
            continue
        for column in columns:
            populated = sum(1 for r in subject_rows if r.get(column) is not None)
            if populated == 0:
                problems.append(
                    f"{subject_type}.{column} is null on all {len(subject_rows)} rows"
                )

    # The observable lead-time columns must be present wherever there is a captured sample --
    # they are what the gate grades against, and the TRUE_ ones are deliberately not.
    sampled = [
        r
        for r in rows
        if r.get("SUBJECT_TYPE") == "SUPPLIER_PART" and (r.get("OBSERVED_DELIVERY_N") or 0) > 1
    ]
    missing = [r["SUBJECT_ID"] for r in sampled if r.get("OBSERVED_SIGMA_DELAY_DAYS") is None]
    if missing:
        problems.append(
            f"{len(missing)} sampled pairs have no OBSERVED_SIGMA_DELAY_DAYS "
            f"(e.g. {missing[0]})"
        )

    return problems


def _fingerprint(data: dict[str, list[dict]]) -> dict[str, str]:
    """Per-table content hash. Cheaper to compare than two full datasets."""
    out: dict[str, str] = {}
    for table, rows in sorted(data.items()):
        payload = json.dumps(rows, sort_keys=True, default=str).encode()
        out[table] = hashlib.sha256(payload).hexdigest()[:16]
    return out


def check_determinism() -> list[str]:
    """§5.7 — two runs from the same seed produce identical rows."""
    first = _fingerprint(dataset.build())
    second = _fingerprint(dataset.build())
    return [
        f"{table} is not deterministic ({first[table]} != {second[table]})"
        for table in first
        if first[table] != second[table]
    ]


def run_all(*, include_determinism: bool = True) -> dict[str, list[str]]:
    """Every §5 assertion. Returns {assertion: problems}; all-empty means the dataset is sound."""
    histories = replenishment.simulate_all()
    records = supplier_facts.delivery_records(histories)
    data = dataset.build()

    results = {
        "5.1 reconciliation": check_reconciliation(histories),
        "5.1 snapshot ties to transactions": check_snapshot_ties_to_transactions(data),
        "5.2 every finding is live": check_every_finding_is_live(histories),
        "5.3 quiet majority": check_quiet_majority(histories),
        "5.4 cohort coverage": check_cohort_coverage(histories),
        "5.5 fallback tiers": check_fallback_tiers(records),
        "5.6 referential integrity": check_referential_integrity(data),
        "5.8 ground truth is gradeable": check_ground_truth_is_gradeable(data),
    }
    if include_determinism:
        results["5.7 determinism"] = check_determinism()
    return results


def failures(results: dict[str, list[str]]) -> list[str]:
    return [f"[{name}] {problem}" for name, problems in results.items() for problem in problems]
