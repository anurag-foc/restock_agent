"""Unit tests for the Teams Adaptive Card the Supervisor's notification sends.

The card builder lives in the MCP app rather than `src/`, because that app is
the authoritative home of the action tools (see CLAUDE.md). It is pure string
and dict work with no Databricks round-trip, so it is worth covering here even
though the rest of that app is not.
"""

import sys
from pathlib import Path

import pytest

MCP_ROOT = Path(__file__).resolve().parent.parent / "mcp-inventory-actions"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

tools = pytest.importorskip("server.tools", reason="MCP app deps (requests, databricks-sdk) not installed")


TWO_CANDIDATES = """## CANDIDATE 1 of 2 -- BOM_CASCADE_RISK

RECOMMENDATION: Purchase 150 units P1015 to WH-026 from SUP-040
DECISION VALUE: Rs 1,27,34,375 (exposure Rs 1,56,25,000 less Rs 28,90,625 to act)
SIGNAL: BOM_CASCADE_RISK | P1015 @ WH-026 | 128 on hand vs 31 safety stock

WHY NOW: Component P1015 shortage blocks 125 units of parent assembly P1002. Second sentence ignored.

IF APPROVED AND WRONG: Rs 5,16,120 (150 x Rs 3,400 = Rs 5,10,000, plus Rs 6,120 holding cost)

## CANDIDATE 2 of 2 -- STOCK_THRESHOLD

RECOMMENDATION: Purchase 1,871 units PRT-027 to WH-037 from SUP-031
DECISION VALUE: Rs 35,26,254 (exposure Rs 49,39,500 less Rs 14,13,246 to act)

WHY NOW: Stock is 267 units below safety stock and the buffer is gone in 8 days.
"""

# Every quote written before the multi-candidate turn protocol looks like this.
LEGACY_SINGLE_BLOCK = """RECOMMENDATION: Transfer 80 units PRT-037 from WH002
DECISION VALUE: Rs 12,000 (exposure Rs 15,000 less Rs 3,000 to act)

WHY NOW: Shortfall is covered by network surplus at no purchase cost.
"""


def _body_texts(card):
    return [b["text"] for b in card["attachments"][0]["content"]["body"]]


def test_header_counts_action_items_not_candidates():
    texts = _body_texts(tools._build_adaptive_card("QT-1", TWO_CANDIDATES, "https://app/x"))
    assert texts[0] == "\U0001f3ed Found 2 action items"
    assert "candidate" not in " ".join(texts).lower()


def test_one_bullet_per_item_with_money_and_reason():
    texts = _body_texts(tools._build_adaptive_card("QT-1", TWO_CANDIDATES, "https://app/x"))
    assert texts[2] == "**1. Purchase 150 units P1015 to WH-026 from SUP-040**"
    assert texts[3].startswith("Rs 1,27,34,375 at stake · Component P1015 shortage blocks")
    # Only the first sentence of WHY NOW -- the card is a nudge, not the report.
    assert "Second sentence ignored" not in texts[3]
    assert texts[4] == "**2. Purchase 1,871 units PRT-027 to WH-037 from SUP-031**"


def test_card_does_not_reprint_the_full_report():
    texts = " ".join(_body_texts(tools._build_adaptive_card("QT-1", TWO_CANDIDATES, "https://app/x")))
    assert "IF APPROVED AND WRONG" not in texts
    assert "SIGNAL:" not in texts


def test_button_is_a_plain_open_url_action():
    actions = tools._build_adaptive_card("QT-1", TWO_CANDIDATES, "https://app/x")["attachments"][0]["content"]["actions"]
    assert actions == [{"type": "Action.OpenUrl", "title": "Review in Databricks", "url": "https://app/x"}]


def test_report_without_candidate_markers_degrades_to_one_item():
    texts = _body_texts(tools._build_adaptive_card("QT-OLD", LEGACY_SINGLE_BLOCK, "https://app/x"))
    assert texts[0] == "\U0001f3ed Found 1 action item"
    assert texts[2] == "**1. Transfer 80 units PRT-037 from WH002**"


def test_empty_report_still_renders_a_card():
    card = tools._build_adaptive_card("QT-EMPTY", "", "https://app/x")
    texts = _body_texts(card)
    assert texts[0] == "\U0001f3ed Found 0 action items"
    assert texts[-1] == "A restock quote is awaiting review."
