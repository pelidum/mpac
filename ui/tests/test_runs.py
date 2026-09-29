"""Tests for /runs/* routes."""

import re
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
# PDF answers cap scales with the model count (guards against OOM on large runs)
# ---------------------------------------------------------------------------


def _wire_pdf_run(mock_stub, n_models: int, sample_size: int):
    """Wire mock_stub for a PDF download of an eval run with ``n_models`` models."""
    run_pb = service_pb2.TestRun(
        id="run-cap",
        test_id="test-1",
        owner="user@example.com",
        status=1,
        sample_size=sample_size,
        models=[
            service_pb2.Model(id=f"m{i}", responder_id=f"m{i}", backend_id="be-1")
            for i in range(n_models)
        ],
    )
    test_pb = service_pb2.Test(id="test-1", name="T", type=1, item_count=sample_size)
    item = service_pb2.TestItem(
        id="item-1", question="q?", answer="a", is_relevant=True
    )
    mock_stub.GetTestRun.return_value = run_pb
    mock_stub.GetTest.return_value = test_pb
    mock_stub.ListTestRunAnswers.return_value = iter(
        [service_pb2.TestRunAnswer(item_id="item-1", model_id="m0", answer="a")]
    )
    mock_stub.GetTestItem.side_effect = lambda req, **kw: item
    mock_stub.BatchGetAttachments.return_value = iter([])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "n_models, sample_size, expected_limit",
    [
        # Few models: capped at the flat item ceiling (PDF_MAX_ANSWER_ITEMS=500).
        (2, 5000, 500),
        # Many models: cell budget (2000) / models shrinks the row count.
        (50, 5000, 40),
        # Very wide runs still keep the minimum floor (PDF_MIN_ANSWER_ITEMS=25).
        (2000, 5000, 25),
        # Never fetch more items than the run actually has.
        (2, 40, 40),
    ],
)
async def test_download_pdf_answer_cap_scales_with_models(
    client, mock_stub, n_models, sample_size, expected_limit
):
    _wire_pdf_run(mock_stub, n_models=n_models, sample_size=sample_size)

    r = await client.get("/runs/run-cap/download/pdf")
    assert r.status_code == 200

    list_req = mock_stub.ListTestRunAnswers.call_args.args[0]
    assert list_req.limit == expected_limit


@pytest.mark.asyncio
async def test_pdf_error_analysis_totals_are_full_run(mock_stub):
    """FP/FN totals in Error Analysis reflect the full run (confusion matrix),
    not the truncated answers sample, even when the answers table is capped."""
    run_pb = service_pb2.TestRun(
        id="run-ea",
        test_id="test-1",
        owner="user@example.com",
        status=1,
        sample_size=50,  # full run has 50 items ...
        models=[
            service_pb2.Model(id="model-a", responder_id="model-a", backend_id="be-1")
        ],
        metrics=[
            service_pb2.TestRun.TestRunMetrics(
                responder_id="model-a",
                precision=0.9,
                recall=0.5,
                f1=0.6,
                true_positives=40,
                true_negatives=8,
                false_positives=5,
                false_negatives=47,
            )
        ],
    )
    test_pb = service_pb2.Test(id="test-1", name="T", type=1, item_count=50)
    item = service_pb2.TestItem(
        id="item-1", question="q?", answer="yes", is_relevant=True
    )
    # ... but only ONE item's answers are returned, so the sample is truncated.
    answer_pbs = [
        service_pb2.TestRunAnswer(
            item_id="item-1",
            model_id="model-a",
            responder_id="model-a",
            answer="no",
            is_correct=False,  # relevant + wrong => false negative
        )
    ]
    mock_stub.GetTestRun.return_value = run_pb
    mock_stub.GetTest.return_value = test_pb
    mock_stub.ListTestRunAnswers.return_value = iter(answer_pbs)
    mock_stub.GetTestItem.side_effect = lambda req, **kw: item
    mock_stub.BatchGetAttachments.return_value = iter([])

    from ui.routers.runs import (
        PDF_MAX_ANSWER_CELLS,
        PDF_MAX_EMBEDDED_IMAGES,
        _fetch_run_report_data,
    )

    with mock.patch("ui.grpc_client._stub", mock_stub):
        ctx = await _fetch_run_report_data(
            "run-ea",
            None,
            num_items=0,
            max_answer_cells=PDF_MAX_ANSWER_CELLS,
            max_images=PDF_MAX_EMBEDDED_IMAGES,
        )

    assert ctx["answers_truncated"] is True
    assert ctx["answers_shown"] == 1
    assert ctx["answers_total"] == 50

    from ui.server import templates

    html = templates.get_template("run_report_pdf.html").render(ctx)
    # Totals are full-run (47 FN, 5 FP), while only what's in the sample is shown.
    assert "False Negatives — showing 1 of 47" in html
    assert "False Positives — showing 0 of 5" in html


# ---------------------------------------------------------------------------
# Performance metrics: TTFT / TPS / latency only
# ---------------------------------------------------------------------------


def _wire_perf_run(mock_stub):
    """A run with one streamed responder and one pre-streaming (legacy) one."""
    run_pb = service_pb2.TestRun(
        id="run-perf",
        test_id="test-1",
        owner="user@example.com",
        status=1,
        sample_size=1,
        models=[
            service_pb2.Model(id="model-a", responder_id="model-a", backend_id="be-1"),
            service_pb2.Model(id="model-b", responder_id="model-b", backend_id="be-1"),
        ],
        metrics=[
            service_pb2.TestRun.TestRunMetrics(
                responder_id="model-a",
                model_id="model-a",
                ttft_p50=0.25,
                output_tps_p50=42.0,
                median_task_duration=1.5,
                task_duration_p95=2.5,
                tokens_per_minute=2520,
                streamed=True,
            ),
            service_pb2.TestRun.TestRunMetrics(
                responder_id="model-b",
                model_id="model-b",
                # Legacy run: prefill-polluted tok/s must not be shown or win.
                output_tps_p50=99.0,
                median_task_duration=0.5,
                task_duration_p95=0.75,
                tokens_per_minute=9999,
            ),
        ],
    )
    test_pb = service_pb2.Test(id="test-1", name="T", type=2, item_count=1)
    item = service_pb2.TestItem(id="item-1", question="q?", choices=["a", "b"])
    mock_stub.GetTestRun.return_value = run_pb
    mock_stub.GetTest.return_value = test_pb
    mock_stub.ListTestRunAnswers.return_value = iter([])
    mock_stub.GetTestItem.side_effect = lambda req, **kw: item
    mock_stub.BatchGetAttachments.return_value = iter([])


@pytest.mark.asyncio
async def test_perf_winners_use_aligned_metrics(mock_stub):
    _wire_perf_run(mock_stub)
    from ui.routers.runs import _fetch_run_report_data

    with mock.patch("ui.grpc_client._stub", mock_stub):
        ctx = await _fetch_run_report_data("run-perf", None, num_items=0)

    winners = ctx["category_winners"]
    assert winners["ttft"] == pytest.approx(0.25)
    assert winners["output_tps"] == pytest.approx(42.0)  # legacy 99 excluded
    assert winners["latency_p50"] == pytest.approx(0.5)
    assert "throughput" not in winners


@pytest.mark.asyncio
async def test_pdf_shows_ttft_tps_latency_only(mock_stub):
    _wire_perf_run(mock_stub)
    from ui.routers.runs import _fetch_run_report_data
    from ui.server import templates

    with mock.patch("ui.grpc_client._stub", mock_stub):
        ctx = await _fetch_run_report_data("run-perf", None, num_items=0)
    html = templates.get_template("run_report_pdf.html").render(ctx)

    assert "TTFT p50" in html
    assert "TPS p50" in html
    assert "0.25s" in html
    assert "42.0" in html
    assert "Throughput" not in html
    assert "99.0" not in html  # legacy TPS hidden
    assert "&mdash;" in html  # legacy TTFT / TPS


@pytest.mark.asyncio
async def test_run_details_shows_ttft_tps_latency_only(client, mock_stub):
    _wire_perf_run(mock_stub)
    r = await client.get("/runs/run-perf")
    assert r.status_code == 200
    # Only visible markup: the page also embeds raw metrics JSON for its charts.
    html = re.sub(r"<script.*?</script>", "", r.text, flags=re.DOTALL)
    assert "TTFT p50" in html
    assert "TPS p50" in html
    assert "Throughput" not in html
    assert "99.0" not in html


# ---------------------------------------------------------------------------
# Answer details: status / error / reasoning / justification / raw response
# ---------------------------------------------------------------------------

_A = service_pb2.TestRunAnswer


def _detail_answers():
    return [
        # New: native reasoning + verbatim raw response.
        _A(item_id="item-1", model_id="model-a", responder_id="model-a", answer="a",
           status=_A.OK, reasoning="native thoughts", raw_response="a"),
        # New: justification from the follow-up call.
        _A(item_id="item-1", model_id="model-b", responder_id="model-b", answer="b",
           status=_A.OK, justification="because b", raw_response="b"),
        # Legacy: error encoded in raw_response prefix + message in reasoning.
        _A(item_id="item-2", model_id="model-a", responder_id="model-a",
           raw_response="[TIMEOUT] Inference request timed out",
           reasoning="Request timeout - inference took too long"),
        # New: failed request.
        _A(item_id="item-2", model_id="model-b", responder_id="model-b",
           status=_A.CONNECTION_ERROR, error="Connection error: refused"),
    ]


def _wire_details_run(mock_stub):
    _wire_perf_run(mock_stub)
    items = {
        "item-1": service_pb2.TestItem(id="item-1", question="q1?", choices=["a", "b"]),
        "item-2": service_pb2.TestItem(id="item-2", question="q2?", choices=["a", "b"]),
    }
    mock_stub.GetTestRun.return_value.sample_size = 2
    mock_stub.ListTestRunAnswers.return_value = iter(_detail_answers())
    mock_stub.GetTestItem.side_effect = lambda req, **kw: items[req.id]


@pytest.mark.asyncio
async def test_report_data_normalizes_legacy_errors(mock_stub):
    _wire_details_run(mock_stub)
    from ui.routers.runs import _fetch_run_report_data

    with mock.patch("ui.grpc_client._stub", mock_stub):
        ctx = await _fetch_run_report_data("run-perf", None, num_items=10)

    responses = {
        (item_id, rk): r
        for item_id, d in ctx["answers"].items()
        for rk, r in d["responses"].items()
    }
    legacy = responses[("item-2", "model-a")]
    assert legacy["status"] == "TIMEOUT"
    assert legacy["error"] == "Request timeout - inference took too long"
    assert legacy["reasoning"] == ""
    assert legacy["raw_response"] == ""
    assert responses[("item-1", "model-a")]["status"] == "OK"
    assert responses[("item-1", "model-b")]["justification"] == "because b"


@pytest.mark.asyncio
async def test_run_details_answer_details(client, mock_stub):
    _wire_details_run(mock_stub)
    r = await client.get("/runs/run-perf")
    assert r.status_code == 200
    html = re.sub(r"<script.*?</script>", "", r.text, flags=re.DOTALL)

    assert 'onclick="openAnswerDetails(this)"' in html
    assert 'data-reasoning="native thoughts"' in html
    assert 'data-justification="because b"' in html
    assert 'data-error="Connection error: refused"' in html
    # Legacy error text is shown as an error, not as reasoning.
    assert 'data-error="Request timeout - inference took too long"' in html
    assert 'data-reasoning="Request timeout' not in html
    assert '<span class="ans-status">TIMEOUT</span>' in html
    assert '<span class="ans-status">CONNECTION ERROR</span>' in html
    assert "openReasoningModal" not in r.text
    assert "openRawResponseModal" not in r.text


@pytest.mark.asyncio
async def test_json_export_normalizes_answers(client, mock_stub):
    _wire_details_run(mock_stub)
    mock_stub.ListTestItems.return_value = iter([])
    r = await client.get("/runs/run-perf/download/json")
    assert r.status_code == 200
    rows = {(x["item_id"], x["responder_id"]): x for x in r.json()["items"]}
    legacy = rows[("item-2", "model-a")]
    assert legacy["status"] == "TIMEOUT"
    assert legacy["raw_response"] == ""
    assert legacy["error"] == "Request timeout - inference took too long"


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
