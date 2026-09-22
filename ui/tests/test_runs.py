"""Tests for /runs/* routes."""

from unittest import mock

import pytest

from server import service_pb2


@pytest.mark.asyncio
async def test_runs_requires_auth(anon_client):
    r = await anon_client.get("/runs", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_runs_page(client, mock_stub):
    mock_stub.ListTestRuns.return_value = iter([])
    r = await client.get("/runs")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_run_create_page(client, mock_stub):
    mock_stub.GetUser.return_value = service_pb2.User(id="user@example.com")
    r = await client.get("/runs/create")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_run_submit_redirect(client, mock_stub):
    mock_stub.CreateTestRun.return_value = iter(
        [service_pb2.TestRunReply(run_id="run-abc")]
    )
    mock_stub.GetTest.return_value = service_pb2.Test(id="test-1", item_count=0)
    mock_stub.ListTestItems.return_value = iter([])
    mock_stub.ListModels.return_value = iter([])
    mock_stub.GetBackend.return_value = service_pb2.Backend(id="be-1")

    r = await client.post(
        "/runs/submit",
        json={"test_id": "test-1", "backend_id": "be-1"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    assert "run-abc" in data.get("redirect_url", "")


@pytest.mark.asyncio
async def test_run_cancel_requires_auth(anon_client):
    r = await anon_client.post(
        "/runs/cancel", json={"stream_id": "x"}, follow_redirects=False
    )
    assert r.status_code in (303, 403)


@pytest.mark.asyncio
async def test_run_detail_not_found(client, mock_stub):
    import grpc

    mock_stub.GetTestRun.side_effect = grpc.RpcError()
    r = await client.get("/runs/nonexistent-run")
    assert r.status_code == 404


# ── PDF download tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_download_pdf_requires_auth(anon_client):
    r = await anon_client.get("/runs/test-run/download/pdf", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_download_pdf_not_found(client, mock_stub):
    import grpc

    mock_stub.GetTestRun.side_effect = grpc.RpcError()
    r = await client.get("/runs/nonexistent/download/pdf")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_pdf_success(client, mock_stub):
    from google.protobuf.timestamp_pb2 import Timestamp

    ts = Timestamp()
    ts.GetCurrentTime()

    run_pb = service_pb2.TestRun(
        id="run-pdf-1",
        test_id="test-1",
        owner="user@example.com",
        status=1,
        sample_size=2,
        total_cost_usd=0.05,
        total_tokens=1000,
        include_reasoning=False,
        models=[
            service_pb2.Model(id="model-a", backend_id="be-1"),
            service_pb2.Model(id="model-b", backend_id="be-1"),
        ],
        metrics=[
            service_pb2.TestRun.TestRunMetrics(
                responder_id="model-a",
                precision=0.9,
                recall=0.85,
                f1=0.874,
                output_tps_p50=42.0,
                median_task_duration=1.2,
                task_duration_p95=2.5,
                tokens_per_minute=2520,
                total_tokens=500,
                total_cost_usd=0.03,
                refusal_error_rate=0.0,
                true_positives=8,
                true_negatives=1,
                false_positives=1,
                false_negatives=0,
            ),
            service_pb2.TestRun.TestRunMetrics(
                responder_id="model-b",
                precision=0.8,
                recall=0.7,
                f1=0.746,
                output_tps_p50=35.0,
                median_task_duration=1.5,
                task_duration_p95=3.0,
                tokens_per_minute=2100,
                total_tokens=500,
                total_cost_usd=0.02,
                refusal_error_rate=0.05,
                true_positives=7,
                true_negatives=1,
                false_positives=2,
                false_negatives=0,
            ),
        ],
    )
    run_pb.created_at_utc.CopyFrom(ts)
    run_pb.completed_at_utc.CopyFrom(ts)

    test_pb = service_pb2.Test(id="test-1", name="My Test", type=1, item_count=2)

    item1 = service_pb2.TestItem(
        id="item-1", question="What is 1+1?", answer="2", is_relevant=True
    )
    item2 = service_pb2.TestItem(
        id="item-2", question="What is 2+2?", answer="4", is_relevant=True
    )

    answer_pbs = [
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="model-a", answer="2", is_correct=True
        ),
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="model-b", answer="3", is_correct=False
        ),
        service_pb2.TestRunAnswer(
            item_id="item-2", model_id="model-a", answer="4", is_correct=True
        ),
        service_pb2.TestRunAnswer(
            item_id="item-2", model_id="model-b", answer="4", is_correct=True
        ),
    ]

    mock_stub.GetTestRun.return_value = run_pb
    mock_stub.GetTest.return_value = test_pb
    mock_stub.ListTestRunAnswers.return_value = iter(answer_pbs)
    mock_stub.GetTestItem.side_effect = lambda req, **kw: (
        item1 if req.id == "item-1" else item2
    )
    mock_stub.BatchGetAttachments.return_value = iter([])

    r = await client.get("/runs/run-pdf-1/download/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Export consensus (CSV wide matrix + JSON additive block)
# ---------------------------------------------------------------------------


def _survey_run_fixtures():
    """A survey run with gpt-4o entered twice plus claude and llama.
    item-1 votes yes/no/yes/yes → consensus "yes", agreement 0.75.
    """
    run_pb = service_pb2.TestRun(
        id="run-consensus-1",
        test_id="test-1",
        owner="user@example.com",
        status=1,
        sample_size=1,
        models=[
            service_pb2.Model(id="gpt-4o", responder_id="gpt-4o", backend_id="be-1"),
            service_pb2.Model(id="gpt-4o", responder_id="gpt-4o#2", backend_id="be-1"),
            service_pb2.Model(id="claude", responder_id="claude", backend_id="be-1"),
            service_pb2.Model(id="llama", responder_id="llama", backend_id="be-1"),
        ],
    )
    test_pb = service_pb2.Test(id="test-1", name="Survey", type=2, item_count=1)
    item1 = service_pb2.TestItem(id="item-1", question="Is the sky blue?")
    answer_pbs = [
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="gpt-4o", responder_id="gpt-4o", answer="yes"
        ),
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="gpt-4o", responder_id="gpt-4o#2", answer="no"
        ),
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="claude", responder_id="claude", answer="yes"
        ),
        service_pb2.TestRunAnswer(
            item_id="item-1", model_id="llama", responder_id="llama", answer="yes"
        ),
    ]
    return run_pb, test_pb, [item1], answer_pbs


def _wire_export_stub(mock_stub, run_pb, test_pb, item_pbs, answer_pbs):
    mock_stub.GetTestRun.return_value = run_pb
    mock_stub.GetTest.return_value = test_pb
    mock_stub.ListTestRunAnswers.return_value = iter(answer_pbs)
    mock_stub.ListTestItems.return_value = iter(item_pbs)


@pytest.mark.asyncio
async def test_download_csv_consensus_matrix(client, mock_stub):
    import csv
    import io

    run_pb, test_pb, items, answers = _survey_run_fixtures()
    _wire_export_stub(mock_stub, run_pb, test_pb, items, answers)

    r = await client.get("/runs/run-consensus-1/download/csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]

    reader = csv.DictReader(io.StringIO(r.text))
    fields = reader.fieldnames
    # The duplicated model yields two distinct responder columns.
    assert "gpt-4o" in fields and "gpt-4o#2" in fields
    assert "claude" in fields and "llama" in fields
    assert "consensus" in fields and "agreement" in fields

    rows = list(reader)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == "item-1"
    assert row["gpt-4o"] == "yes"
    assert row["gpt-4o#2"] == "no"
    assert row["claude"] == "yes"
    assert row["consensus"] == "yes"
    assert float(row["agreement"]) == 0.75
    assert row["num_responders"] == "4"
    # Survey: no ground truth to compare against.
    assert row["consensus_correct"] == ""


@pytest.mark.asyncio
async def test_download_json_consensus_block(client, mock_stub):
    import json

    run_pb, test_pb, items, answers = _survey_run_fixtures()
    _wire_export_stub(mock_stub, run_pb, test_pb, items, answers)

    r = await client.get("/runs/run-consensus-1/download/json")
    assert r.status_code == 200
    data = json.loads(r.text)

    # items[] stays flat and back-compatible, now carrying responder_id.
    assert isinstance(data["items"], list)
    assert len(data["items"]) == 4
    assert data["items"][0]["responder_id"] == "gpt-4o"

    consensus = data["consensus_by_item"]
    assert len(consensus) == 1
    entry = consensus[0]
    assert entry["item_id"] == "item-1"
    assert entry["consensus_answer"] == "yes"
    assert entry["agreement"] == 0.75
    assert entry["num_responders"] == 4
    assert entry["vote_counts"] == {"yes": 3, "no": 1}
    assert entry["consensus_correct"] is None  # survey → no ground truth


def test_compute_item_consensus_unit():
    from ui.routers.runs import _compute_item_consensus

    unanimous = _compute_item_consensus({"a": "yes", "b": "yes"})
    assert unanimous["consensus_answer"] == "yes"
    assert unanimous["agreement"] == 1.0
    assert unanimous["num_responders"] == 2

    # Blank / refusal answers are excluded from the vote.
    partial = _compute_item_consensus({"a": "yes", "b": "", "c": "no", "d": "yes"})
    assert partial["consensus_answer"] == "yes"
    assert partial["num_responders"] == 3
    assert abs(partial["agreement"] - 2 / 3) < 1e-9

    empty = _compute_item_consensus({"a": "", "b": None})
    assert empty["consensus_answer"] is None
    assert empty["agreement"] == 0.0
    assert empty["num_responders"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
