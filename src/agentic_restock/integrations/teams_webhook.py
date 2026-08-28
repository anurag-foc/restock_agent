"""Teams Adaptive Card integration for the Manufacturing Inventory Intelligence Engine.

Sends intelligence-enriched Restock Quote notifications to a Microsoft Teams
channel via an Incoming Webhook URL.

Usage:
    # Set TEAMS_WEBHOOK_URL env var, then call:
    result = send_quote_card(
        quote_id="QT-20260828-C8C9",
        candidates=[...],
        supervisor_summary="### Restock Quote...",
        review_app_url="https://workspace.databricks.com/apps/restock-review?quote_id=QT-20260828-C8C9",
        webhook_url=None,            # reads TEAMS_WEBHOOK_URL env var
        dry_run=False,               # set True in unit tests
    )
    # result["teams_message_id"] can be stored in quote_metadata

Design notes:
- The card carries a "Review in Databricks" link button (not an in-card Approve
  button). Final approval is deferred to the Databricks review surface, which
  always reflects live catalog data rather than the snapshot in the card.
- Dry-run / no-webhook mode logs the card JSON and returns a mock message ID
  without raising, so the Lakeflow job never fails due to a missing webhook
  configuration.
- Adaptive Cards 1.4 schema is used for broad Teams client compatibility.
"""

import json
import os
import re
import logging
import datetime
import uuid
from typing import Any

import urllib.request
import urllib.error

log = logging.getLogger(__name__)

# ── Public constants ──────────────────────────────────────────────────────────

URGENCY_COLORS = {
    "CRITICAL": "attention",   # red
    "HIGH": "warning",         # yellow / orange
    "MEDIUM": "accent",        # blue
    "LOW": "good",             # green
}

SIGNAL_LABELS = {
    "STOCK_THRESHOLD": "🔴 Stock Threshold Breached",
    "PREDICTED_STOCKOUT": "🟡 Predicted Stockout (Proactive)",
    "BOM_CASCADE_RISK": "🟠 BOM Cascade Risk",
}


# ── Card builder ──────────────────────────────────────────────────────────────

def _urgency_color(urgency: str) -> str:
    return URGENCY_COLORS.get(urgency.upper(), "accent")


def _signal_label(signal_type: str) -> str:
    return SIGNAL_LABELS.get(signal_type, signal_type)


def _truncate(text: str, max_chars: int = 500) -> str:
    """Trim long text for Teams card display — cards have hard size limits."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n\n_… (see Databricks for full report)_"


def build_adaptive_card(
    quote_id: str,
    candidates: list[dict],
    supervisor_summary: str,
    review_app_url: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a Teams Adaptive Card payload for a Restock Quote.

    Args:
        quote_id:           Business key (e.g. "QT-20260828-C8C9").
        candidates:         List of candidate dicts from the multi-signal scanner.
        supervisor_summary: Markdown intelligence report from the Supervisor Agent.
        review_app_url:     Deep link to the Databricks Review App for this quote.
        generated_at:       ISO-8601 timestamp string (defaults to now UTC).

    Returns:
        A dict representing the full Teams Incoming Webhook JSON payload.
    """
    generated_at = generated_at or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # --- Header urgency & signal aggregation ---
    urgencies = [c.get("initial_urgency", "HIGH") for c in candidates]
    top_urgency = "CRITICAL" if "CRITICAL" in urgencies else urgencies[0] if urgencies else "HIGH"
    signal_types = list({c.get("signal_type", "STOCK_THRESHOLD") for c in candidates})
    top_color = _urgency_color(top_urgency)

    # --- Candidates table rows ---
    table_rows: list[dict] = []
    for c in candidates:
        signal = c.get("signal_type", "STOCK_THRESHOLD")
        urg = c.get("initial_urgency", "HIGH")
        label = _signal_label(signal)
        days_info = f" · {c['days_to_stockout']}d to stockout" if c.get("days_to_stockout") else ""
        assembly_info = f" · threatens {c['threatened_assembly']}" if c.get("threatened_assembly") else ""
        table_rows.append({
            "type": "TableRow",
            "cells": [
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": c.get("item_id", "—"), "weight": "Bolder", "size": "Small", "wrap": True}],
                },
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": c.get("warehouse_id", "—"), "size": "Small", "wrap": True}],
                },
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": f"{c.get('current_stock_qty', '?')} / {c.get('reorder_point_qty', '?')}", "size": "Small", "wrap": True}],
                },
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": f"{urg}{days_info}{assembly_info}", "color": _urgency_color(urg), "weight": "Bolder", "size": "Small", "wrap": True}],
                },
            ],
        })

    # --- Intelligence summary (truncated for card display) ---
    display_summary = _truncate(
        # Strip markdown headers that look noisy in card text blocks
        re.sub(r"^#{1,4} ", "", supervisor_summary, flags=re.MULTILINE),
        max_chars=600,
    )

    # --- Assemble the Adaptive Card body ---
    card_body: list[dict] = [
        # Banner
        {
            "type": "TextBlock",
            "text": f"🏭 Manufacturing Inventory Intelligence Alert",
            "weight": "Bolder",
            "size": "Large",
            "color": top_color,
            "wrap": True,
        },
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{"type": "TextBlock", "text": "Quote ID", "weight": "Bolder", "size": "Small", "isSubtle": True}],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [{"type": "TextBlock", "text": quote_id, "weight": "Bolder", "size": "Small"}],
                },
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{"type": "TextBlock", "text": generated_at, "isSubtle": True, "size": "Small"}],
                },
            ],
        },
        # Signal type chips
        {
            "type": "TextBlock",
            "text": "  ".join(_signal_label(s) for s in signal_types),
            "size": "Small",
            "wrap": True,
            "spacing": "None",
        },
        {"type": "Separator"},
        # Candidates table header
        {
            "type": "TextBlock",
            "text": "📦 Flagged Candidates",
            "weight": "Bolder",
            "size": "Medium",
            "spacing": "Medium",
        },
        {
            "type": "Table",
            "columns": [
                {"width": 2},
                {"width": 1},
                {"width": 2},
                {"width": 2},
            ],
            "rows": [
                {
                    "type": "TableRow",
                    "style": "emphasis",
                    "cells": [
                        {"type": "TableCell", "items": [{"type": "TextBlock", "text": "Part", "weight": "Bolder", "size": "Small"}]},
                        {"type": "TableCell", "items": [{"type": "TextBlock", "text": "WH", "weight": "Bolder", "size": "Small"}]},
                        {"type": "TableCell", "items": [{"type": "TextBlock", "text": "Stock / Reorder", "weight": "Bolder", "size": "Small"}]},
                        {"type": "TableCell", "items": [{"type": "TextBlock", "text": "Signal", "weight": "Bolder", "size": "Small"}]},
                    ],
                },
                *table_rows,
            ],
            "firstRowAsHeader": True,
        },
        {"type": "Separator"},
        # Intelligence summary
        {
            "type": "TextBlock",
            "text": "🧠 Intelligence Summary",
            "weight": "Bolder",
            "size": "Medium",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": display_summary,
            "wrap": True,
            "size": "Small",
            "fontType": "Monospace",
        },
        {"type": "Separator", "spacing": "Medium"},
        # Decision note
        {
            "type": "TextBlock",
            "text": "⚠️ **Action required in Databricks.** Review the full 4-layer intelligence report and approve or reject this quote in the Databricks Review App.",
            "wrap": True,
            "size": "Small",
            "color": "Warning",
        },
    ]

    # --- Action button ---
    actions = [
        {
            "type": "Action.OpenUrl",
            "title": "📋 Review in Databricks",
            "url": review_app_url,
            "style": "positive",
        }
    ]

    # --- Full Adaptive Card wrapper ---
    adaptive_card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": card_body,
        "actions": actions,
    }

    # --- Teams Incoming Webhook payload ---
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": adaptive_card,
            }
        ],
    }


# ── Sender ────────────────────────────────────────────────────────────────────

def send_quote_card(
    quote_id: str,
    candidates: list[dict],
    supervisor_summary: str,
    review_app_url: str,
    webhook_url: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build and send a Restock Quote Adaptive Card to Teams.

    Args:
        quote_id:           Business key for the quote.
        candidates:         List of candidate dicts from the multi-signal scanner.
        supervisor_summary: Full markdown intelligence report text.
        review_app_url:     Deep link to the Databricks Review App.
        webhook_url:        Teams Incoming Webhook URL. Falls back to
                            ``TEAMS_WEBHOOK_URL`` env var. If neither is set
                            the function runs in dry-run mode automatically.
        dry_run:            If True, log the card payload without HTTP call.

    Returns:
        dict with keys:
          - ``teams_message_id`` (str | None): Synthetic or real message ID.
          - ``sent_at`` (str): ISO-8601 timestamp when the card was dispatched.
          - ``dry_run`` (bool): Whether the send was real or simulated.
          - ``card_payload`` (dict): The raw payload sent (or that would have been sent).
    """
    webhook_url = webhook_url or os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        dry_run = True
        log.warning(
            "TEAMS_WEBHOOK_URL not configured — running in dry-run mode. "
            "Set the environment variable to enable real Teams notifications."
        )

    payload = build_adaptive_card(
        quote_id=quote_id,
        candidates=candidates,
        supervisor_summary=supervisor_summary,
        review_app_url=review_app_url,
    )

    sent_at = datetime.datetime.utcnow().isoformat() + "Z"
    teams_message_id: str | None = None

    if dry_run:
        log.info("Teams Adaptive Card (dry-run):\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"[Teams dry-run] Card for quote {quote_id} logged (no HTTP call made).")
        teams_message_id = f"dry-run-{uuid.uuid4().hex[:8]}"
    else:
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                # Teams Incoming Webhooks return "1" on success (not JSON)
                log.info("Teams webhook HTTP %s: %s", resp.status, raw)
                teams_message_id = f"teams-{quote_id}-{uuid.uuid4().hex[:8]}"
                print(f"✅ Teams Adaptive Card sent for quote {quote_id}. Message ID: {teams_message_id}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            log.error("Teams webhook HTTP error %s: %s", exc.code, body)
            raise RuntimeError(f"Teams webhook returned HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            log.error("Teams webhook URL error: %s", exc.reason)
            raise RuntimeError(f"Teams webhook connection failed: {exc.reason}") from exc

    return {
        "teams_message_id": teams_message_id,
        "sent_at": sent_at,
        "dry_run": dry_run,
        "card_payload": payload,
    }


# ── Deep-link builder ─────────────────────────────────────────────────────────

def build_review_app_url(
    quote_id: str,
    workspace_url: str | None = None,
    app_path: str = "restock-review",
) -> str:
    """Construct the Databricks Review App deep-link URL for a given quote.

    Pattern from architecture §5:
        https://<workspace>.databricks.com/apps/<app_path>?quote_id=<quote_id>

    Args:
        quote_id:      Business key.
        workspace_url: Base Databricks workspace URL, e.g.
                       ``https://adb-1234.azuredatabricks.net``.
                       Falls back to ``DATABRICKS_HOST`` env var.
        app_path:      Name/path segment of the Databricks App.

    Returns:
        Fully qualified URL string.
    """
    host = workspace_url or os.environ.get("DATABRICKS_HOST", "https://your-workspace.databricks.com")
    host = host.rstrip("/")
    return f"{host}/apps/{app_path}?quote_id={quote_id}"
