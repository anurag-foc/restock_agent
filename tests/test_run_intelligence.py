"""Tests for the single-turn Supervisor invocation.

The MCP approval protocol is fiddly and its failure mode is a confusing server error rather than
an exception you can read ("Invalid message sequence. The approval response was in an unexpected
position."), so it is tested against a fake client rather than discovered against a live endpoint.
"""

import pytest

from agentic_restock.jobs import run_intelligence as R


class FakeClient:
    """Stands in for a WorkspaceClient, recording each request body it is sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.api_client = self

    def do(self, method, path, body=None):
        self.requests.append(body)
        return self._responses.pop(0)


def _text(value):
    return {"output": [{"content": [{"output_text": value}]}]}


def _approval(request_id, name):
    return {"output": [{"type": "mcp_approval_request", "id": request_id, "name": name}]}


def _done(value, *names):
    return {
        "output": [
            *({"type": "mcp_call", "name": n} for n in names),
            {"content": [{"output_text": value}]},
        ]
    }


# --- response parsing -------------------------------------------------------


def test_text_is_extracted_from_the_responses_api_shape():
    """Buried at output[].content[].output_text — not where Chat Completions puts it."""
    assert R.extract_text(_text("the report")) == "the report"


def test_text_is_joined_across_output_items():
    response = {
        "output": [
            {"content": [{"output_text": "first"}]},
            {"content": [{"output_text": "second"}]},
        ]
    }
    assert R.extract_text(response) == "first\nsecond"


def test_missing_text_returns_empty_rather_than_raising():
    assert R.extract_text({}) == ""
    assert R.extract_text({"output": []}) == ""
    assert R.extract_text({"output": [{"type": "mcp_call", "name": "x"}]}) == ""


def test_tool_calls_are_detected():
    assert R.called_tools(_done("done", "persist_quote", "send_human_review")) == [
        "persist_quote",
        "send_human_review",
    ]


# --- the approval round-trip ------------------------------------------------


def test_a_turn_with_no_approval_returns_immediately():
    client = FakeClient([_done("report", "send_human_review")])
    result = R.invoke(client, "endpoint", "brief")
    assert result.approval_rounds == 0
    assert len(client.requests) == 1


def test_an_approval_request_is_answered_and_the_turn_continues():
    client = FakeClient(
        [_approval("req-1", "persist_quote"), _done("report", "persist_quote")]
    )
    result = R.invoke(client, "endpoint", "brief")
    assert result.approval_rounds == 1
    assert result.text == "report"
    assert len(client.requests) == 2


def test_the_whole_transcript_is_resent_because_the_endpoint_is_stateless():
    """`previous_response_id` chaining does not work here. The continuation must carry everything
    sent so far PLUS every output item from the prior response, verbatim."""
    client = FakeClient(
        [_approval("req-1", "persist_quote"), _done("report", "persist_quote")]
    )
    R.invoke(client, "endpoint", "the brief")

    continuation = client.requests[1]["input"]
    assert continuation[0] == {"role": "user", "content": "the brief"}
    # The prior response's output item, unmodified.
    assert {"type": "mcp_approval_request", "id": "req-1", "name": "persist_quote"} in continuation
    # Followed by the approval response.
    assert continuation[-1] == {
        "type": "mcp_approval_response",
        "approval_request_id": "req-1",
        "approve": True,
    }


def test_multiple_approvals_in_one_response_are_all_answered():
    both = {
        "output": [
            {"type": "mcp_approval_request", "id": "a", "name": "persist_quote"},
            {"type": "mcp_approval_request", "id": "b", "name": "send_human_review"},
        ]
    }
    client = FakeClient([both, _done("report", "persist_quote", "send_human_review")])
    R.invoke(client, "endpoint", "brief")
    responses = [
        item
        for item in client.requests[1]["input"]
        if item.get("type") == "mcp_approval_response"
    ]
    assert {r["approval_request_id"] for r in responses} == {"a", "b"}


def test_endless_approval_requests_are_bounded():
    """A model that keeps asking must not loop forever against a paid endpoint."""
    client = FakeClient([_approval(f"req-{i}", "persist_quote") for i in range(20)])
    with pytest.raises(RuntimeError, match="after 5 rounds"):
        R.invoke(client, "endpoint", "brief")


# --- verification -----------------------------------------------------------


def test_a_missing_persist_call_is_a_hard_failure():
    """A quote header with no part-lines is an empty approval screen. That exact state shipped
    once with a full report, zero rows, and a job reporting SUCCESS."""
    client = FakeClient([_done("report", "send_human_review")])
    with pytest.raises(AssertionError, match="persist_quote was never called"):
        R.run(client, "endpoint", brief="b", expected_lines=3)


def test_a_missing_notification_is_a_hard_failure():
    client = FakeClient([_done("report", "persist_quote")])
    with pytest.raises(AssertionError, match="send_human_review was never called"):
        R.run(client, "endpoint", brief="b", expected_lines=1)


def test_an_empty_report_is_a_hard_failure():
    client = FakeClient([_done("", "persist_quote", "send_human_review")])
    with pytest.raises(AssertionError, match="no report text"):
        R.run(client, "endpoint", brief="b", expected_lines=1)


def test_a_run_with_no_quote_lines_still_requires_a_notification():
    """Nothing to persist is a legitimate state; telling nobody is not."""
    client = FakeClient([_done("report", "send_human_review")])
    result = R.run(client, "endpoint", brief="b", expected_lines=0)
    assert result.text == "report"


def test_a_complete_turn_passes_verification():
    client = FakeClient([_done("report", "persist_quote", "send_human_review")])
    result = R.run(client, "endpoint", brief="b", expected_lines=2)
    assert set(result.tool_calls) == {"persist_quote", "send_human_review"}


def test_the_failure_message_names_which_tools_did_run():
    """So the first debugging question — did it call anything at all — is already answered."""
    client = FakeClient([_done("report", "send_human_review")])
    with pytest.raises(AssertionError, match="send_human_review"):
        R.run(client, "endpoint", brief="b", expected_lines=1)


def test_the_run_summary_is_json_for_the_job_log():
    import json

    client = FakeClient([_done("report", "persist_quote", "send_human_review")])
    result = R.run(client, "endpoint", brief="b", expected_lines=1)
    summary = json.loads(
        R.summarise(result, {"considered": 188, "selected": 4, "selected_by_type": {"X": 4}})
    )
    assert summary["considered"] == 188
    assert summary["raised"] == 4
    assert summary["tools_called"] == ["persist_quote", "send_human_review"]
