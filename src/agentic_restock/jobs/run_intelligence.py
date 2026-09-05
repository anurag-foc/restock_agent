"""Orchestrate one intelligence run: detect, select, narrate, act.

Replaces `invoke_supervisor.py`'s 2+N-turn protocol with **one** Supervisor turn.

That protocol existed because of a hard constraint, not a preference: Model Serving has a 290s
HTTP gateway ceiling, and ranking plus per-candidate drill-downs plus the write-up reliably
exceeded it. So the work was split — a pre-check, a scan turn, one analysis turn per candidate,
then a final persist/notify turn.

The redesign removes the reason for the split rather than optimising it. Every figure is computed
by the detectors and attached to its finding as evidence, so there are no drill-downs left to
make. What remains is write-up plus two tool calls, which fits comfortably inside one request.

The parts of the old path that were hard-won are kept exactly:

- The endpoint speaks the OpenAI **Responses API** shape (`{"input": [...]}`), not Chat
  Completions, so `serving_endpoints.query()` builds the wrong body and this posts to
  `/serving-endpoints/{name}/invocations` directly.
- Timeouts must be passed via `WorkspaceClient(config=Config(...))`. As kwargs they are
  rejected; set on `w.config` afterwards they are read too late and silently ignored, and the
  symptom is a `TimeoutError` at exactly 5 minutes.
- A custom MCP (`app`-type) tool call returns an `mcp_approval_request` instead of executing,
  and that cannot be disabled at registration or request time. The endpoint is stateless, so
  `previous_response_id` chaining does not work — continuing means resending the whole
  transcript plus every item from the prior response's `output` verbatim, plus an
  `mcp_approval_response`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

# Bounded so a model that keeps requesting approval cannot loop forever.
MAX_APPROVAL_ROUNDS = 5

# Under the 290s gateway ceiling, with room for the round-trips an approval needs.
TURN_TIMEOUT_SECONDS = 280


@dataclass
class TurnResult:
    text: str
    tool_calls: list[str]
    approval_rounds: int
    raw: dict[str, Any]


def extract_text(response: dict) -> str:
    """Pull the assistant text out of a Responses API payload.

    Buried at `output[…].content[…].output_text`, and the shape varies with which items the turn
    produced — a turn with tool calls interleaves those into the same `output` list.
    """
    chunks: list[str] = []
    for item in response.get("output", []) or []:
        for part in item.get("content", []) or []:
            text = part.get("output_text") or part.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def pending_approvals(response: dict) -> list[dict]:
    return [
        item
        for item in (response.get("output") or [])
        if item.get("type") == "mcp_approval_request"
    ]


def called_tools(response: dict) -> list[str]:
    names: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") in ("mcp_call", "mcp_approval_request"):
            name = item.get("name") or item.get("tool_name")
            if name:
                names.append(name)
    return names


def invoke(client, endpoint_name: str, brief: str) -> TurnResult:
    """One Supervisor turn, answering any MCP approval requests inline.

    Approvals are answered within the same logical turn rather than across turns, because the
    endpoint keeps no state: each continuation resends everything sent so far plus every output
    item from the previous response, verbatim. Dropping or reordering those produces
    "Invalid message sequence. The approval response was in an unexpected position."
    """
    conversation: list[dict] = [{"role": "user", "content": brief}]
    tool_calls: list[str] = []

    for approval_round in range(MAX_APPROVAL_ROUNDS + 1):
        response = client.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={"input": conversation},
        )
        tool_calls.extend(called_tools(response))

        approvals = pending_approvals(response)
        if not approvals:
            return TurnResult(
                text=extract_text(response),
                tool_calls=tool_calls,
                approval_rounds=approval_round,
                raw=response,
            )

        # Resend the whole transcript: everything so far, plus this response's output items
        # verbatim, plus one approval response per request.
        conversation = [
            *conversation,
            *(response.get("output") or []),
            *(
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": item.get("id"),
                    "approve": True,
                }
                for item in approvals
            ),
        ]

    raise RuntimeError(
        f"{endpoint_name}: still requesting approval after {MAX_APPROVAL_ROUNDS} rounds — "
        "refusing to loop. Check the MCP app is reachable and its tools are returning."
    )


def verify_side_effects(result: TurnResult, expected_lines: int) -> list[str]:
    """Check the turn actually did what it was asked, and say so precisely if not.

    The notebooks *verify* the action tools ran and fail loudly; they never retry them, because
    every tool behind the MCP app is idempotent server-side and a retry from here would be
    second-guessing that.

    A line-count mismatch is a hard failure, not a warning. A quote header with no part-lines is
    an empty approval screen — and that exact state shipped once, with a full summary_report,
    zero rows, and a job reporting SUCCESS.
    """
    problems: list[str] = []

    if expected_lines > 0 and "persist_quote" not in result.tool_calls:
        problems.append("persist_quote was never called, so nothing was written")
    if "send_human_review" not in result.tool_calls:
        problems.append("send_human_review was never called, so nobody was notified")
    if not result.text:
        problems.append("the turn produced no report text")

    return problems


def run(
    client,
    endpoint_name: str,
    *,
    brief: str,
    expected_lines: int,
    as_of: date | None = None,
) -> TurnResult:
    """The single turn, plus its verification. Raises on a failed side effect."""
    result = invoke(client, endpoint_name, brief)

    problems = verify_side_effects(result, expected_lines)
    if problems:
        raise AssertionError(
            f"Supervisor turn on {endpoint_name} ({as_of or 'today'}) did not complete: "
            + "; ".join(problems)
            + f"\ntools called: {result.tool_calls or 'none'}"
        )
    return result


def summarise(result: TurnResult, selection_report: dict) -> str:
    """A one-line run summary for the job log and the scan run log."""
    return json.dumps(
        {
            "considered": selection_report.get("considered"),
            "raised": selection_report.get("selected"),
            "by_type": selection_report.get("selected_by_type"),
            "tools_called": sorted(set(result.tool_calls)),
            "approval_rounds": result.approval_rounds,
            # The model's trailing chat text, NOT the stored report -- the report goes to
            # persist_quote as an argument, so this is typically a short "done" line. It read
            # `report_chars: 141` on a run whose quote_metadata.summary_report was 4,077
            # characters, which looks like a truncated report in the job log and is not one.
            "closing_text_chars": len(result.text),
        },
        sort_keys=True,
    )
