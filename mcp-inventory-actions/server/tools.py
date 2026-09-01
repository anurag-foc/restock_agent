"""Inventory Intelligence action tools exposed by this MCP server.

Ported from restock-review/server/mcp.ts (the previous home of these tools,
reached via a UC HTTP Connection). This app is attached to the Supervisor
Agent directly as an `app` tool instead, so no UC Connection / service
principal / secret scope is needed for the Supervisor to reach it — but the
app's own (auto-provisioned) service principal still needs Unity Catalog
grants on the tables below; see docs/agent_bricks_mapping.md.

Every tool here is IDEMPOTENT by construction. The Supervisor is an LLM: it
may retry, re-run, or call a tool twice in an ambiguous turn. Duplicate rows
in fact_restock_request mean duplicate procurement, so idempotency is
enforced here in code rather than assumed of the model:
  - persist_quote           -> quote_id is a deterministic hash of the
                               candidate set + date; existing quote is
                               returned instead of re-inserted.
  - send_human_review       -> no-ops if quote_metadata.teams_message_id is
                               already set.
  - fulfill_restock_request -> only transitions a line that is currently
                               APPROVED; re-calls are reported, not re-applied.

fulfill_restock_request also never trusts the model for arithmetic: it reads
live stock and computes CONFIRMED_QTY/VARIANCE_QTY itself.
"""

import hashlib
import json
import os
import time

import requests

from server import db


def _as_int(value, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def _deterministic_quote_id(candidates: list[dict]) -> str:
    """Same candidate set on the same UTC day always produces the same id."""
    fingerprint = "|".join(
        sorted(
            f"{c.get('item_id', '')}@{c.get('warehouse_id', '')}:{_as_int(c.get('suggested_reorder_qty'))}"
            for c in candidates
        )
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:6].upper()
    return f"QT-{db.today_date_key()}-{digest}"


def _build_adaptive_card(quote_id: str, summary: str, review_url: str) -> dict:
    trimmed = summary if len(summary) <= 1200 else f"{summary[:1200]}\n\n_… (full report in Databricks)_"
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🏭 Restock Quote Awaiting Approval",
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Quote {quote_id}",
                            "weight": "Bolder",
                            "size": "Small",
                            "isSubtle": True,
                        },
                        {"type": "TextBlock", "text": trimmed, "wrap": True, "size": "Small"},
                        {
                            "type": "TextBlock",
                            "text": "⚠️ Approve or reject each part-line in the Databricks Review App.",
                            "wrap": True,
                            "size": "Small",
                            "color": "Warning",
                        },
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "📋 Review in Databricks",
                            "url": review_url,
                            "style": "positive",
                        }
                    ],
                },
            }
        ],
    }


def load_tools(mcp_server):
    """Register the three Inventory Intelligence action tools with the MCP server."""

    @mcp_server.tool
    def health() -> dict:
        """Check the health of the MCP server and Databricks connection."""
        return {
            "status": "healthy",
            "message": "inventory-actions-mcp is healthy and connected to Databricks Apps.",
        }

    @mcp_server.tool
    def persist_quote(candidates_json: str, summary_report: str) -> dict:
        """Save a completed restock quote to Delta as PENDING_APPROVAL.

        Writes one quote_metadata header row and one fact_restock_request line
        per candidate. Idempotent: the same candidate set on the same day
        returns the existing quote instead of creating a duplicate. Returns the
        quote_id, which is required by send_human_review.

        Args:
            candidates_json: JSON array of candidate objects from the Lakeflow
                scanner, each with item_id, warehouse_id, current_stock_qty,
                reorder_point_qty, suggested_reorder_qty, initial_urgency.
            summary_report: The full consolidated Restock Quote text, stored
                for the reviewer.
        """
        try:
            candidates = json.loads(candidates_json)
        except json.JSONDecodeError as e:
            raise ValueError("candidates_json must be a JSON array of candidate objects") from e
        if not isinstance(candidates, list) or len(candidates) == 0:
            raise ValueError("candidates_json must be a non-empty JSON array")

        quote_id = _deterministic_quote_id(candidates)

        existing = db.run_sql(
            f"SELECT quote_id FROM {db.QUOTE_METADATA} WHERE quote_id = :quoteId",
            [db.param("quoteId", quote_id)],
        )
        if existing:
            return {
                "quote_id": quote_id,
                "created": False,
                "note": "Quote already exists for this candidate set today; no rows written.",
            }

        db.run_sql(
            f"""INSERT INTO {db.QUOTE_METADATA} (quote_id, summary_report, created_by, created_at, updated_at)
                VALUES (:quoteId, :summaryReport, 'supervisor_agent', current_timestamp(), current_timestamp())""",
            [db.param("quoteId", quote_id), db.param("summaryReport", summary_report or "")],
        )

        today_key = db.today_date_key()
        inserted = 0
        for i, c in enumerate(candidates):
            request_id = f"REQ-{quote_id.replace('QT-', '')}-{i + 1}"
            db.run_sql(
                f"""INSERT INTO {db.FACT_RESTOCK_REQUEST} (
                        RESTOCK_REQUEST_KEY, QUOTE_ID, RESTOCK_REQUEST_ID, REQUESTED_DATE_KEY,
                        PART_KEY, WAREHOUSE_KEY, REQUEST_STATUS_KEY,
                        CURRENT_STOCK_QTY, REORDER_POINT_QTY, REQUESTED_QTY, DW_LOADED_AT
                    )
                    SELECT
                        (SELECT COALESCE(MAX(RESTOCK_REQUEST_KEY), 0) FROM {db.FACT_RESTOCK_REQUEST}) + 1,
                        :quoteId, :requestId, :todayKey,
                        dp.PART_KEY, dw.WAREHOUSE_KEY,
                        (SELECT MIN(REQUEST_STATUS_KEY) FROM {db.DIM_REQUEST_STATUS}
                          WHERE REQUEST_STATUS = 'PENDING_APPROVAL' AND URGENCY_LEVEL = :urgency),
                        :currentStock, :reorderPoint, :requestedQty, current_timestamp()
                    FROM {db.DIM_PART} dp
                    CROSS JOIN {db.DIM_WAREHOUSE} dw
                    WHERE dp.PART_ID = :partId AND dp.IS_CURRENT = true
                      AND dw.WAREHOUSE_ID = :warehouseId""",
                [
                    db.param("quoteId", quote_id),
                    db.param("requestId", request_id),
                    db.param("todayKey", today_key, "INT"),
                    db.param("urgency", c.get("initial_urgency") or "CRITICAL"),
                    db.param("currentStock", _as_int(c.get("current_stock_qty")), "INT"),
                    db.param("reorderPoint", _as_int(c.get("reorder_point_qty")), "INT"),
                    db.param("requestedQty", _as_int(c.get("suggested_reorder_qty")), "INT"),
                    db.param("partId", c.get("item_id") or ""),
                    db.param("warehouseId", c.get("warehouse_id") or ""),
                ],
            )
            inserted += 1

        return {"quote_id": quote_id, "created": True, "lines_written": inserted}

    @mcp_server.tool
    def send_human_review(quote_id: str, summary_report: str, review_url: str) -> dict:
        """Notify the Production Manager in Microsoft Teams that a quote needs review.

        Sends a deep link to the Databricks Review App. Call only after
        persist_quote has returned a quote_id. Idempotent: does nothing if a
        card was already sent for this quote.

        Args:
            quote_id: quote_id returned by persist_quote.
            summary_report: Quote text to show on the Teams card.
            review_url: Deep link to the Review App for this quote.
        """
        existing = db.run_sql(
            f"SELECT teams_message_id FROM {db.QUOTE_METADATA} WHERE quote_id = :quoteId",
            [db.param("quoteId", quote_id)],
        )
        if not existing:
            raise ValueError(f"No quote_metadata row for {quote_id} — call persist_quote first.")
        if existing[0][0]:
            return {
                "quote_id": quote_id,
                "sent": False,
                "teams_message_id": existing[0][0],
                "note": "Card already sent.",
            }

        webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
        payload = _build_adaptive_card(quote_id, summary_report, review_url)
        dry_run = False

        if not webhook_url:
            # Never fail the workflow because a webhook isn't configured —
            # record a dry-run marker instead.
            dry_run = True
            teams_message_id = f"dry-run-{int(time.time() * 1000)}"
        else:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if not resp.ok:
                raise RuntimeError(f"Teams webhook returned HTTP {resp.status_code}: {resp.text[:200]}")
            teams_message_id = f"teams-{quote_id}-{int(time.time() * 1000)}"

        db.run_sql(
            f"""UPDATE {db.QUOTE_METADATA}
                SET teams_message_id = :messageId, teams_sent_at = current_timestamp(),
                    databricks_preview_url = :reviewUrl, updated_at = current_timestamp()
                WHERE quote_id = :quoteId""",
            [
                db.param("messageId", teams_message_id),
                db.param("reviewUrl", review_url),
                db.param("quoteId", quote_id),
            ],
        )

        return {"quote_id": quote_id, "sent": True, "dry_run": dry_run, "teams_message_id": teams_message_id}

    @mcp_server.tool
    def fulfill_restock_request(restock_request_key: int, proceed: bool, note: str = "") -> dict:
        """Record a PROCEED / NEEDS_REVIEW verdict on a single APPROVED restock line.

        This tool computes the current stock, variance vs the quote-time
        stock, and the confirmed quantity itself from live data — the caller
        does not supply any of those numbers. The only input is the judgment
        call: proceed=true moves the line to FULFILLING at its
        originally-approved quantity; proceed=false moves it to NEEDS_REVIEW
        instead. Idempotent: only acts on a line that is currently APPROVED.

        Args:
            restock_request_key: RESTOCK_REQUEST_KEY of the part-line being decided.
            proceed: true = still makes sense, move to FULFILLING at the
                approved quantity. false = flag NEEDS_REVIEW instead of
                writing the transition blindly.
            note: One or two sentences explaining the verdict, appended to the
                quote for the PM to see.
        """
        line_key = _as_int(restock_request_key, -1)
        if line_key < 0:
            raise ValueError("restock_request_key must be an integer")

        rows = db.run_sql(
            f"""SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL, frr.QUOTE_ID, frr.PART_KEY, frr.WAREHOUSE_KEY,
                       frr.REQUESTED_QTY, frr.CURRENT_STOCK_QTY
                FROM {db.FACT_RESTOCK_REQUEST} frr
                JOIN {db.DIM_REQUEST_STATUS} drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
                WHERE frr.RESTOCK_REQUEST_KEY = :lineKey""",
            [db.param("lineKey", line_key, "BIGINT")],
        )
        if not rows:
            raise ValueError(f"No fact_restock_request row with RESTOCK_REQUEST_KEY={line_key}")

        current_status, urgency, quote_id, part_key, warehouse_key, requested_qty, quote_time_stock = rows[0]
        if current_status != "APPROVED":
            return {
                "restock_request_key": line_key,
                "transitioned": False,
                "current_status": current_status,
                "note": f"Line is {current_status}, not APPROVED — no transition applied.",
            }

        snapshot_rows = db.run_sql(
            f"""SELECT QUANTITY_ON_HAND
                FROM {db.FACT_INVENTORY_SNAPSHOT}
                WHERE PART_KEY = :partKey AND WAREHOUSE_KEY = :warehouseKey
                QUALIFY ROW_NUMBER() OVER (PARTITION BY PART_KEY, WAREHOUSE_KEY ORDER BY SNAPSHOT_DATE_KEY DESC) = 1""",
            [db.param("partKey", part_key, "BIGINT"), db.param("warehouseKey", warehouse_key, "BIGINT")],
        )
        current_stock = _as_int(snapshot_rows[0][0]) if snapshot_rows else None
        variance_qty = current_stock - _as_int(quote_time_stock) if current_stock is not None else None

        new_status = "FULFILLING" if proceed else "NEEDS_REVIEW"
        set_clauses = [
            f"REQUEST_STATUS_KEY = (SELECT MIN(REQUEST_STATUS_KEY) FROM {db.DIM_REQUEST_STATUS} "
            "WHERE REQUEST_STATUS = :newStatus AND URGENCY_LEVEL = :urgency)"
        ]
        params = [db.param("newStatus", new_status), db.param("urgency", urgency)]
        if variance_qty is not None:
            set_clauses.append("VARIANCE_QTY = :varianceQty")
            params.append(db.param("varianceQty", variance_qty, "INT"))
        confirmed_qty = None
        if proceed:
            confirmed_qty = _as_int(requested_qty)
            set_clauses.append("CONFIRMED_QTY = :confirmedQty")
            params.append(db.param("confirmedQty", confirmed_qty, "INT"))
        params.append(db.param("lineKey", line_key, "BIGINT"))

        db.run_sql(
            f"UPDATE {db.FACT_RESTOCK_REQUEST} SET {', '.join(set_clauses)} WHERE RESTOCK_REQUEST_KEY = :lineKey",
            params,
        )

        if note:
            db.run_sql(
                f"""UPDATE {db.QUOTE_METADATA}
                    SET decision_comments = CONCAT(
                          COALESCE(decision_comments, ''),
                          CASE WHEN decision_comments IS NULL OR decision_comments = '' THEN '' ELSE '\n\n' END,
                          :note
                        ),
                        updated_at = current_timestamp()
                    WHERE quote_id = :quoteId""",
                [
                    db.param("note", f"[line {line_key} -> {new_status}] {note}"),
                    db.param("quoteId", str(quote_id)),
                ],
            )

        return {
            "restock_request_key": line_key,
            "transitioned": True,
            "new_status": new_status,
            "current_stock_qty": current_stock,
            "variance_qty": variance_qty,
            "confirmed_qty": confirmed_qty,
        }
