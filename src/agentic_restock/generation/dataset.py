"""Assembles every generated table into one mapping.

One place builds the dataset, so the self-assertions and the loader cannot drift apart — a
checker that reassembles the data its own way is checking something other than what gets loaded.

Keys are logical table names; `TARGETS` maps each to its Unity Catalog schema and whether the
load has to resolve surrogate keys against a dimension we are not regenerating.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_restock.generation import (
    bom,
    contracts,
    decisions,
    entities,
    ground_truth,
    procurement,
    production,
    replenishment,
    snapshots,
    supplier_facts,
)


@dataclass(frozen=True)
class Target:
    """Where a generated table lands, and how."""

    schema: str
    table: str
    # Columns prefixed with '_' are inputs to a load-time join, not table columns. Currently
    # only fact_restock_request needs one: dim_request_status is a legitimate 17-row dimension
    # with hash surrogate keys that we are NOT regenerating, so status is carried by name.
    resolve_status: bool = False


TARGETS: dict[str, Target] = {
    "dim_plant": Target("dim", "dim_plant"),
    "dim_production_line": Target("dim", "dim_production_line"),
    "dim_warehouse": Target("dim", "dim_warehouse"),
    "dim_vehicle_model": Target("dim", "dim_vehicle_model"),
    "dim_part": Target("dim", "dim_part"),
    "dim_supplier": Target("dim", "dim_supplier"),
    "dim_bom": Target("supply_chain_analytics", "dim_bom"),
    "dim_model_bom": Target("supply_chain_analytics", "dim_model_bom"),
    "dim_supplier_contract": Target("supply_chain_analytics", "dim_supplier_contract"),
    "fact_inventory_snapshot": Target("supply_chain_analytics", "fact_inventory_snapshot"),
    "fact_inventory_transaction": Target("supply_chain_analytics", "fact_inventory_transaction"),
    "fact_supplier_delivery": Target("supply_chain_analytics", "fact_supplier_delivery"),
    "fact_supplier_quality": Target("supply_chain_analytics", "fact_supplier_quality"),
    "fact_procurement": Target("supply_chain_analytics", "fact_procurement"),
    "fact_restock_request": Target(
        "supply_chain_analytics", "fact_restock_request", resolve_status=True
    ),
    "fact_production_execution": Target("manufacturing_analytics", "fact_production_execution"),
    "fact_vehicle_build": Target("manufacturing_analytics", "fact_vehicle_build"),
    "sim_ground_truth": Target("supply_chain_analytics", "sim_ground_truth"),
}

# Tables whose rows we replace wholesale. `dim_date` and `dim_request_status` are deliberately
# absent: both are legitimate populated dimensions, and dim_date only needs its IS_HOLIDAY flags
# filled in (an UPDATE, not a replace).
PRUNE_TARGETS = list(TARGETS)


def build() -> dict[str, list[dict]]:
    """Generate the whole dataset in memory. Deterministic; ~2s."""
    histories = replenishment.simulate_all()
    delivery_records = supplier_facts.delivery_records(histories)

    return {
        "dim_plant": entities.plants(),
        "dim_production_line": entities.production_lines(),
        "dim_warehouse": entities.warehouses(),
        "dim_vehicle_model": entities.vehicle_models(),
        "dim_part": entities.parts(),
        "dim_supplier": entities.suppliers(),
        "dim_bom": bom.part_bom(),
        "dim_model_bom": bom.model_bom(),
        "dim_supplier_contract": contracts.contracts(),
        "fact_inventory_snapshot": snapshots.snapshot_rows(histories),
        "fact_inventory_transaction": snapshots.transaction_rows(histories),
        "fact_supplier_delivery": supplier_facts.delivery_rows(delivery_records),
        "fact_supplier_quality": supplier_facts.quality_rows(delivery_records),
        "fact_procurement": procurement.open_po_rows(histories),
        "fact_restock_request": decisions.restock_request_rows(histories),
        "fact_production_execution": production.execution_rows(),
        "fact_vehicle_build": production.vehicle_build_rows(),
        "sim_ground_truth": ground_truth.all_rows(histories, delivery_records),
    }


def all_keys(rows: list[dict]) -> list[str]:
    """Union of keys across ALL rows, in first-seen order.

    Not `rows[0].keys()`. `sim_ground_truth` holds two row shapes — PAIR rows carrying burn
    parameters and SUPPLIER_PART rows carrying lead-time parameters — and reading the schema
    from the first row silently dropped every supplier-only column. The load then padded them
    with NULL, which emptied the half of Phase 2's gate that grades the lead-time estimator, and
    did so without any error.
    """
    keys: list[str] = []
    for row in rows:
        keys.extend(key for key in row if key not in keys)
    return keys


def table_columns(rows: list[dict]) -> list[str]:
    """Real table columns — anything prefixed with '_' is a load-time helper, not a column."""
    return [key for key in all_keys(rows) if not key.startswith("_")]


def row_counts(data: dict[str, list[dict]]) -> dict[str, int]:
    return {name: len(rows) for name, rows in data.items()}
