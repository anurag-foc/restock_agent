"""C — the consequence model: which assemblies cannot be built, and what that is worth.

Computed **at the parent, attributed to children**. That ordering is the entire point, and
getting it backwards produces two defects that the current implementation has:

- **Inflated exposure.** Accumulating upward from each child hangs the parent's full
  `value_at_risk` on every child independently, so three components blocking one engine assembly
  each claim the whole engine's value. Ranking then compares a number that triple-counts.
- **A false recommendation.** "Buy component X and ₹8.9 cr of output is unblocked" is simply
  untrue when components Y and Z bind at the same level. The action is the whole binding *set*,
  and a detector that reports one child has told the PM something that will not work.

A part that is *both* below its own safety stock and cascade-binding gets the cascade
consequence, not the cost of the parts. The superseded board excluded exactly that case from its
cascade join (`WHERE on_hand >= safety_stock`), which priced the single most urgent situation in
the system — short *and* blocking A-CRITICAL output — at a few thousand rupees.

Pure functions over plain dicts and lists: no Spark, no SQL, so the Phase 3 gate runs locally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BomEdge:
    parent_part_id: str
    child_part_id: str
    qty_per_unit: float


@dataclass(frozen=True)
class ParentCascade:
    """One threatened assembly. `value_at_risk` is counted once, here."""

    parent_part_id: str
    warehouse_id: str
    required_units: float
    parent_on_hand: int
    buildable_from_children: int
    supply_units: int
    units_blocked: int
    parent_unit_cost: float
    value_at_risk: float
    binding_children: tuple[str, ...] = field(default_factory=tuple)
    child_buildable: dict[str, int] = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return self.units_blocked > 0


def multiplier_index(edges: list[BomEdge]) -> dict[tuple[str, str], float]:
    """(ancestor, descendant) -> units of descendant per unit of ancestor.

    Walks every path so multi-level BOMs are handled: a component two levels down consumes
    `qty(parent→sub) x qty(sub→component)` per parent. A single-level lookup would silently
    treat a 2-per-sub component in a 2-per-parent sub as 1-per-parent, halving its requirement.
    """
    children: dict[str, list[BomEdge]] = {}
    for edge in edges:
        children.setdefault(edge.parent_part_id, []).append(edge)

    index: dict[tuple[str, str], float] = {}

    def walk(ancestor: str, node: str, multiplier: float, seen: frozenset[str]) -> None:
        for edge in children.get(node, []):
            if edge.child_part_id in seen:
                continue  # a cyclic BOM is bad data, not something to recurse forever on
            total = multiplier * edge.qty_per_unit
            key = (ancestor, edge.child_part_id)
            # Keep the largest path multiplier: if a part is reachable by two routes it is
            # consumed by both, and the binding constraint is the heavier requirement.
            index[key] = max(index.get(key, 0.0), total)
            walk(ancestor, edge.child_part_id, total, seen | {edge.child_part_id})

    for parent in children:
        walk(parent, parent, 1.0, frozenset({parent}))

    return index


def descendants(edges: list[BomEdge], parent_part_id: str) -> list[str]:
    index = multiplier_index(edges)
    return sorted(child for (ancestor, child) in index if ancestor == parent_part_id)


def build_parent_cascade(
    *,
    parent_part_id: str,
    warehouse_id: str,
    required_units: float,
    parent_on_hand: int,
    parent_unit_cost: float,
    availability: dict[str, int],
    edges: list[BomEdge],
    multipliers: dict[tuple[str, str], float] | None = None,
) -> ParentCascade | None:
    """Whether this assembly can meet its plan, and what is blocking it.

    `availability` is `on_hand + on_order` per child at this warehouse — a child with stock
    already inbound is a waiting problem, not a shortage, and counting only on-hand would raise
    a cascade against a part that is covered.

    Returns None when the parent has no BOM here; a parent with children but no plan still
    returns a row, because "not blocked" is worth recording.
    """
    del multipliers  # superseded by the recursion below; see the note in `producible`

    children_of: dict[str, list[BomEdge]] = {}
    for edge in edges:
        children_of.setdefault(edge.parent_part_id, []).append(edge)

    if parent_part_id not in children_of:
        return None

    child_buildable: dict[str, int] = {}
    binding_leaves: list[str] = []

    def producible(node: str, seen: frozenset[str]) -> int | None:
        """How many units of `node` can be made available: its own stock, plus what its children
        can still be assembled into.

        **Recursive, not flattened.** An earlier version pre-multiplied every descendant against
        the parent and took a single global MIN. That treats a sub-assembly and its own
        components as independent constraints, when they are additive for that branch: 1011
        sub-assemblies on hand PLUS however many more can be built from the components. The
        flattened version reported the sub-assembly as the binding item, so the actionable
        components -- the parts a PM would actually buy -- never surfaced.
        """
        own = availability.get(node)
        edges_here = [e for e in children_of.get(node, []) if e.child_part_id not in seen]

        if not edges_here:
            return own  # a leaf constrains only by its own stock

        from_children: int | None = None
        for edge in edges_here:
            child_capacity = producible(edge.child_part_id, seen | {edge.child_part_id})
            if child_capacity is None or edge.qty_per_unit <= 0:
                continue
            capacity = math.floor(child_capacity / edge.qty_per_unit)
            child_buildable[edge.child_part_id] = capacity
            from_children = capacity if from_children is None else min(from_children, capacity)

        if from_children is None:
            return own
        return (own or 0) + from_children

    from_children_total: int | None = None
    for edge in children_of[parent_part_id]:
        capacity = producible(edge.child_part_id, frozenset({parent_part_id, edge.child_part_id}))
        if capacity is None or edge.qty_per_unit <= 0:
            continue
        units = math.floor(capacity / edge.qty_per_unit)
        child_buildable[edge.child_part_id] = units
        from_children_total = units if from_children_total is None else min(
            from_children_total, units
        )

    if from_children_total is None:
        return None

    buildable_from_children = from_children_total
    supply = parent_on_hand + buildable_from_children
    units_blocked = max(math.ceil(required_units) - supply, 0)

    # The immediate binding children, then descended to the leaves that actually constrain them
    # -- those are the parts a recommendation can name as buyable.
    immediate = sorted(
        child
        for child, buildable in child_buildable.items()
        if buildable == buildable_from_children and child in {e.child_part_id for e in children_of[parent_part_id]}
    )

    def descend(node: str, seen: frozenset[str]) -> list[str]:
        """Follow the lowest-capacity child down to the leaves that can actually be bought.

        Descends unconditionally while children exist. A node's own stock is fixed, so raising
        its capacity means buying further down — an earlier version stopped whenever a node held
        more stock than its children could add, which reported the sub-assembly as the action
        when the thing to buy was the component beneath it.
        """
        edges_here = [e for e in children_of.get(node, []) if e.child_part_id not in seen]
        if not edges_here:
            return [node]

        floor_value = min(child_buildable.get(e.child_part_id, 0) for e in edges_here)
        out: list[str] = []
        for edge in edges_here:
            if child_buildable.get(edge.child_part_id, 0) == floor_value:
                out.extend(descend(edge.child_part_id, seen | {edge.child_part_id}))
        return out or [node]

    for child in immediate:
        binding_leaves.extend(descend(child, frozenset({parent_part_id, child})))

    binding = tuple(sorted(set(binding_leaves))) if binding_leaves else tuple(immediate)

    return ParentCascade(
        parent_part_id=parent_part_id,
        warehouse_id=warehouse_id,
        required_units=float(required_units),
        parent_on_hand=parent_on_hand,
        buildable_from_children=buildable_from_children,
        supply_units=supply,
        units_blocked=units_blocked,
        parent_unit_cost=float(parent_unit_cost),
        value_at_risk=units_blocked * float(parent_unit_cost),
        binding_children=binding if units_blocked > 0 else (),
        child_buildable=child_buildable,
    )


def child_consequence(cascades: list[ParentCascade]) -> dict[tuple[str, str], dict]:
    """(child, warehouse) -> the production consequence of that child running out.

    **Attribution, not division.** A binding child's consequence is the parent's *full*
    `value_at_risk`, because that is what is genuinely at stake if it runs out — while
    `value_at_risk` itself is only ever summed once, at the parent. Those are different
    questions and conflating them is what produced triple-counted exposure.

    A child binding two parents carries the larger consequence, and both parents are named so a
    report can say which output is at risk rather than quoting a bare number.
    """
    out: dict[tuple[str, str], dict] = {}
    for cascade in cascades:
        if not cascade.is_blocked:
            continue
        for child in cascade.binding_children:
            key = (child, cascade.warehouse_id)
            existing = out.get(key)
            if existing is None or cascade.value_at_risk > existing["value_at_risk"]:
                out[key] = {
                    "value_at_risk": cascade.value_at_risk,
                    "threatened_parent_part_id": cascade.parent_part_id,
                    "units_blocked": cascade.units_blocked,
                    "co_binding_children": tuple(
                        c for c in cascade.binding_children if c != child
                    ),
                }
    return out


def total_value_at_risk(cascades: list[ParentCascade]) -> float:
    """Summed once per parent — the figure a plant manager can report upward."""
    return sum(c.value_at_risk for c in cascades)
