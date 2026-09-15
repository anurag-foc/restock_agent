"""Turning the report into something a person can read, without letting the model do it.

Two jobs, and they are separated because only one of them is safe to hand to an LLM.

**Names.** Every figure in a report was already computed, but every *subject* was still printed
as a database key: `transfer 2582 units of P0021 from WH002 to WH001`. Nobody can picture that.
The names exist in the dimension tables and were simply never read. Resolving them is a pure
substitution over text the detectors already built, so it works across all eight scanners
without any of them changing -- and it happens in code, never in the prose. The one live
incident where the model reached for a part *name* in place of a `PART_ID` wrote a report with
zero part-lines, so name handling is exactly the thing that must not be delegated.

The ID is kept in brackets after the name. A PM cross-referencing an ERP screen needs it, and
dropping it would trade one unreadable report for one that cannot be checked.

**Verification.** The model writes prose. Today nothing checks that prose against the figures it
was given -- the pipeline verifies that the tools were *called*, not that the sentences are
true. That is the same "trust the instruction" posture that produced three fabrications, and it
is why this module extracts every number from what the model wrote and checks each one against
the set that was actually computed. A figure that is not in the set was invented.

Verification is what makes it safe to let the model write MORE, not less: a bigger writing brief
plus an output check is a stronger guarantee than a small writing brief and no check at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Business keys as the detectors emit them: P0021, WH002, SUP015. Matched on a word boundary so
# a key embedded in a longer token is left alone.
_KEY_PATTERN = re.compile(r"\b((?:P|WH|SUP)\d{3,})\b")

# Every number in a piece of prose, including Indian-grouped figures (2,95,83,118), decimals and
# percentages. The currency symbol and separators are stripped before comparison so `Rs 2,95,83,118`
# and `29583117.84` are recognised as the same claim.
_NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Words that carry a figure without writing one. "doubled" and "halved" are claims about
# magnitude that no allowed-figure check can catch, so they are refused outright rather than
# approximately verified.
_BANNED_MAGNITUDE_WORDS = ("doubl", "tripl", "halv", "quadrupl")


@dataclass(frozen=True)
class NameBook:
    """Business key -> human name, for parts, warehouses and suppliers.

    Built once per run from the dimension tables and passed down. Empty by default so every
    caller -- and every test -- keeps working without a cluster, degrading to the IDs that were
    printed before rather than to blanks.
    """

    parts: dict[str, str] = field(default_factory=dict)
    warehouses: dict[str, str] = field(default_factory=dict)
    suppliers: dict[str, str] = field(default_factory=dict)

    def name_for(self, key: str) -> str | None:
        return self.parts.get(key) or self.warehouses.get(key) or self.suppliers.get(key)

    def label(self, key: str) -> str:
        """`Caliper Group (P0021)`, or just the key when the name is unknown."""
        name = self.name_for(key)
        return f"{name} ({key})" if name else key

    @property
    def is_empty(self) -> bool:
        return not (self.parts or self.warehouses or self.suppliers)


EMPTY_NAMES = NameBook()


def humanise(text: str, names: NameBook) -> str:
    """Replace every business key in `text` with `Name (KEY)`.

    A substitution rather than a rewrite: the detectors' sentences are already correct and
    already carry the right figures, so the readable version has to be reachable without
    rebuilding eight scanners' worth of string-building -- and without giving anything the
    chance to restate a number on the way through.

    Idempotent: a key already followed by its own bracketed form is left alone, so running this
    over text that has been through it once does not produce `Caliper Group (Caliper Group
    (P0021))`.
    """
    if not text or names.is_empty:
        return text

    def swap(match: re.Match[str]) -> str:
        key = match.group(1)
        name = names.name_for(key)
        if not name:
            return key
        # Already rendered: "... Caliper Group (P0021) ..." -- the key sits inside brackets
        # immediately after its own name.
        before = match.string[: match.start()]
        if before.rstrip().endswith(f"{name} ("):
            return key
        return f"{name} ({key})"

    return _KEY_PATTERN.sub(swap, text)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def numbers_in(text: str) -> list[float]:
    """Every number in a piece of prose, separators removed."""
    out: list[float] = []
    for raw in _NUMBER_PATTERN.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def allowed_figures(evidence: dict, extra: list[float] | None = None) -> set[float]:
    """Every figure the model is permitted to state for one finding.

    Derived from the evidence it was handed, plus the variants a correct writer would naturally
    produce: a probability written as a percentage, a rupee figure rounded to lakh or crore, and
    an integer rendering of a float. Being generous here is deliberate -- the check exists to
    catch *invention*, and a gate that fires on legitimate rounding would be turned off within a
    week, which is worse than no gate.
    """
    allowed: set[float] = set()

    def add(value: float) -> None:
        allowed.add(round(value, 4))
        allowed.add(float(round(value)))  # 100.77 stated as 101
        allowed.add(round(value, 1))
        allowed.add(round(value, 2))
        if value != 0:
            # Same money at the scale a person would write it.
            allowed.add(round(value / 100_000, 2))  # lakh
            allowed.add(round(value / 100_000, 1))
            allowed.add(round(value / 10_000_000, 2))  # crore
            allowed.add(round(value / 10_000_000, 1))
        if 0 <= value <= 1:
            allowed.add(round(value * 100, 1))  # 0.049 -> 4.9
            allowed.add(float(round(value * 100)))  # 0.049 -> 5

    for value in evidence.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            add(float(value))
        elif isinstance(value, str):
            for found in numbers_in(value):
                add(found)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    add(float(item))

    for value in extra or []:
        add(float(value))

    # Small integers are ordinals, counts and list positions ("1 of 4", "two sites"). Refusing
    # them would fail on "the second warehouse" and teach nobody anything.
    for small in range(13):
        allowed.add(float(small))
    return allowed


@dataclass(frozen=True)
class Verdict:
    ok: bool
    invented: list[float]
    reason: str = ""


def verify_prose(text: str, evidence: dict, extra: list[float] | None = None) -> Verdict:
    """Does every figure in `text` appear among the figures actually computed?

    Returns a verdict rather than raising. A scan that produces duller prose beats a scan that
    produces nothing at 07:00, so the caller falls back to the deterministic sentence and logs
    it -- the failure has to be visible without being fatal.
    """
    lowered = (text or "").lower()
    for word in _BANNED_MAGNITUDE_WORDS:
        if word in lowered:
            return Verdict(
                ok=False,
                invented=[],
                reason=f"states a magnitude in words ({word}...), which cannot be checked",
            )

    allowed = allowed_figures(evidence, extra)
    invented = [n for n in numbers_in(text) if round(n, 4) not in allowed]
    if invented:
        return Verdict(
            ok=False,
            invented=invented,
            reason="figures not present in the evidence: "
            + ", ".join(f"{n:g}" for n in invented[:5]),
        )
    return Verdict(ok=True, invented=[])


# ---------------------------------------------------------------------------
# Reading the names
# ---------------------------------------------------------------------------


def build_name_query(gold_catalog: str | None = None, dim_schema: str | None = None) -> str:
    """One query for all three dimensions, as (kind, key, name) rows.

    A single UNION rather than three reads because the caller wants one object, and because the
    three tables disagree about `IS_CURRENT` -- `dim_warehouse` does not have the column at all,
    which is the kind of detail that turns three tidy reads into three special cases.
    """
    from agentic_restock.config import qualified_dim_table

    part = qualified_dim_table("dim_part", gold_catalog, dim_schema)
    warehouse = qualified_dim_table("dim_warehouse", gold_catalog, dim_schema)
    supplier = qualified_dim_table("dim_supplier", gold_catalog, dim_schema)
    return f"""
    SELECT 'part' AS KIND, PART_ID AS KEY, PART_NAME AS NAME
    FROM {part} WHERE IS_CURRENT = true AND PART_NAME IS NOT NULL
    UNION ALL
    SELECT 'warehouse', WAREHOUSE_ID, WAREHOUSE_NAME
    FROM {warehouse} WHERE WAREHOUSE_NAME IS NOT NULL
    UNION ALL
    SELECT 'supplier', SUPPLIER_ID, SUPPLIER_NAME
    FROM {supplier} WHERE IS_CURRENT = true AND SUPPLIER_NAME IS NOT NULL
    """.strip()


def names_from_rows(rows: list[dict] | None) -> NameBook:
    """Build a `NameBook` from the query above. A failed read degrades to IDs, never to blanks."""
    if not rows:
        return EMPTY_NAMES
    parts: dict[str, str] = {}
    warehouses: dict[str, str] = {}
    suppliers: dict[str, str] = {}
    buckets = {"part": parts, "warehouse": warehouses, "supplier": suppliers}
    for row in rows:
        kind = str(row.get("KIND") or row.get("kind") or "")
        key = row.get("KEY") or row.get("key")
        name = row.get("NAME") or row.get("name")
        if kind in buckets and key and name:
            buckets[kind][str(key)] = str(name)
    return NameBook(parts=parts, warehouses=warehouses, suppliers=suppliers)


# ---------------------------------------------------------------------------
# Checking a whole report
# ---------------------------------------------------------------------------

# The model writes every item in one turn, so the report has to be split back into its items to
# be checked against the right evidence. Both markers are accepted: quotes written before the
# intelligence redesign say CANDIDATE.
_ITEM_SPLIT = re.compile(r"^##\s+(?:ACTION ITEM|CANDIDATE)\s+\d+", re.MULTILINE)

# Only the model-written line is checked. Every other line was substituted in from figures that
# were computed, so checking them would be checking our own arithmetic against itself -- and a
# gate that reports failures it caused itself is one nobody reads.
_MODEL_WRITTEN = re.compile(r"^WHY NOW:\s*(.+)$", re.MULTILINE)


def report_items(report: str) -> list[str]:
    """Split a written report back into its per-item blocks."""
    if not report:
        return []
    chunks = _ITEM_SPLIT.split(report)
    # Everything before the first marker is the model's preamble, not an item.
    return [c for c in chunks[1:] if c.strip()]


def verify_report(report: str, findings: list) -> list[tuple[int, Verdict]]:
    """Check each item's model-written sentence against that item's own evidence.

    Returns `(item_index, verdict)` for failures only, so an empty list means the report states
    nothing that was not measured.

    Pairs items to findings positionally. If the model emitted a different number of items than
    were raised, that is itself the finding worth reporting -- a report with the wrong item count
    has already gone wrong in a way no figure check will describe.
    """
    items = report_items(report)
    problems: list[tuple[int, Verdict]] = []

    if len(items) != len(findings):
        problems.append(
            (
                0,
                Verdict(
                    ok=False,
                    invented=[],
                    reason=f"report has {len(items)} items, {len(findings)} were raised",
                ),
            )
        )
        return problems

    for index, (item, finding) in enumerate(zip(items, findings), start=1):
        written = _MODEL_WRITTEN.search(item)
        if not written:
            continue  # nothing model-written in this item; nothing to check
        verdict = verify_prose(
            written.group(1),
            finding.evidence,
            extra=[finding.exposure, finding.decision_value, finding.consequence,
                   finding.action_cost],
        )
        if not verdict.ok:
            problems.append((index, verdict))
    return problems
