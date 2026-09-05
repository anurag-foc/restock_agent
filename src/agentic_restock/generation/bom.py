"""Bill of materials — 3 levels, plus the model→assembly bridge.

Two tables:

- `dim_model_bom` (new, replica-only): vehicle model → top-level assembly. Without this there is
  no path from a production plan (expressed in models, in `fact_production_execution`) to a part
  requirement, and the BOM-cascade detector has nothing to explode.
  See docs/schema_changes_gold_dev_analytics.md §2.1.
- `dim_bom`: assembly → sub-assembly, and sub-assembly → component. Because a sub-assembly
  appears as both a `component_part_id` and an `fg_part_id`, the table has genuine multi-level
  links and a recursive explosion is exercised. The replica's 5 pre-existing rows were flat,
  with zero such links.

Assignment is formulaic and deterministic (fixed by sorted position), never random. Every
assembly gets at least 2 sub-assemblies and every sub-assembly at least 2 components, which is
what leaves room for the co-binding-children construction (F2) to be built on top in
`scenarios.py`.
"""

from __future__ import annotations

from functools import cache

from agentic_restock.generation.entities import parts, vehicle_models

ASSEMBLY_COUNT = 12
SUBASSEMBLY_COUNT = 28
COMPONENT_COUNT = 60

# Assemblies consumed per vehicle. Most are one-per-vehicle; a couple are not, so the cascade
# arithmetic has to actually multiply rather than pass a quantity through.
_QTY_PER_VEHICLE = {
    "Brake System Assembly": 4,
    "Suspension Assembly": 4,
    "Seat Assembly": 2,
}

_MODELS_PER_ASSEMBLY = 5


@cache
def _parts_by_level() -> tuple[list[dict], list[dict], list[dict]]:
    all_parts = parts()
    return (
        [p for p in all_parts if p["BOM_LEVEL"] == 0],
        [p for p in all_parts if p["BOM_LEVEL"] == 1],
        [p for p in all_parts if p["BOM_LEVEL"] == 2],
    )


@cache
def model_bom() -> list[dict]:
    """dim_model_bom: each model consumes 5 top-level assemblies."""
    assemblies, _, _ = _parts_by_level()
    models = vehicle_models()
    rows: list[dict] = []
    for m_idx, model in enumerate(models):
        for k in range(_MODELS_PER_ASSEMBLY):
            assembly = assemblies[(m_idx + k) % ASSEMBLY_COUNT]
            rows.append(
                {
                    "MODEL_ID": model["VEHICLE_MODEL_ID"],
                    "FG_PART_ID": assembly["PART_ID"],
                    "QTY_PER_VEHICLE": _QTY_PER_VEHICLE.get(assembly["PART_NAME"], 1),
                    "EFFECTIVE_FROM": "2021-04-01",
                    "EFFECTIVE_TO": None,
                    "IS_CURRENT": True,
                }
            )
    return rows


@cache
def part_bom() -> list[dict]:
    """dim_bom: assembly -> sub-assembly, and sub-assembly -> component."""
    assemblies, subs, components = _parts_by_level()
    rows: list[dict] = []

    for i, sub in enumerate(subs):
        parent = assemblies[i % ASSEMBLY_COUNT]
        rows.append(
            {
                "fg_part_id": parent["PART_ID"],
                "component_part_id": sub["PART_ID"],
                "qty_per_unit": 1 + (i % 2),
            }
        )

    for j, component in enumerate(components):
        parent = subs[j % SUBASSEMBLY_COUNT]
        rows.append(
            {
                "fg_part_id": parent["PART_ID"],
                "component_part_id": component["PART_ID"],
                "qty_per_unit": 1 + (j % 4),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Structure helpers -- used by scenarios.py to build the cascade findings
# ---------------------------------------------------------------------------


@cache
def children_of(part_id: str) -> list[str]:
    """Direct children of a part in dim_bom."""
    return [r["component_part_id"] for r in part_bom() if r["fg_part_id"] == part_id]


def qty_per_unit(parent_id: str, child_id: str) -> int:
    for r in part_bom():
        if r["fg_part_id"] == parent_id and r["component_part_id"] == child_id:
            return r["qty_per_unit"]
    raise KeyError(f"{child_id} is not a child of {parent_id}")


@cache
def descendants_of(part_id: str) -> list[str]:
    """All parts beneath `part_id`, recursively — the set a cascade explosion must reach."""
    out: list[str] = []
    frontier = children_of(part_id)
    while frontier:
        nxt: list[str] = []
        for child in frontier:
            out.append(child)
            nxt.extend(children_of(child))
        frontier = nxt
    return out


@cache
def multiplier_to_ancestor(child_id: str, ancestor_id: str) -> int | None:
    """Units of `child_id` consumed per unit of `ancestor_id`, multiplied along the path.

    None when there is no path. This is what makes co-binding constructible: two children bind
    at the same level when `available / multiplier` matches, not when their raw quantities do —
    so a scenario that gives both children the same stock quantity only co-binds by accident,
    and only while both multipliers happen to be 1.
    """
    if child_id == ancestor_id:
        return 1
    for direct_child in children_of(ancestor_id):
        if direct_child == child_id:
            return qty_per_unit(ancestor_id, direct_child)
        deeper = multiplier_to_ancestor(child_id, direct_child)
        if deeper is not None:
            return qty_per_unit(ancestor_id, direct_child) * deeper
    return None


@cache
def models_using(part_id: str) -> list[str]:
    """Models whose BOM includes this top-level assembly."""
    return [r["MODEL_ID"] for r in model_bom() if r["FG_PART_ID"] == part_id]


def units_per_vehicle(model_id: str, part_id: str) -> int:
    """Assemblies of `part_id` consumed per unit of `model_id`."""
    for r in model_bom():
        if r["MODEL_ID"] == model_id and r["FG_PART_ID"] == part_id:
            return r["QTY_PER_VEHICLE"]
    raise KeyError(f"{part_id} not in {model_id}'s BOM")
