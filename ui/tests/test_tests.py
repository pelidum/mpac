"""Tests for /tests/* import and export routes."""

import csv
import io
import json
import zipfile

import pytest

from server import service_pb2


def _csv_bytes(rows: list[dict], fieldnames: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _zip_bytes(manifest: dict, attachments: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in (attachments or {}).items():
            zf.writestr(f"attachments/{name}", data)
    return buf.getvalue()


def _wire_create(mock_stub):
    mock_stub.CreateTest.return_value = service_pb2.Test(id="imported-1")
    mock_stub.CreateTestItem.return_value = service_pb2.TestItem(id="item-x")
    mock_stub.CreateAttachment.return_value = service_pb2.FileAttachment(id="att-1")


# ── page rendering ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_details_page_renders(client, mock_stub):
    """Regression: the detail page's query-kwarg links ('Run Test' -> test_id,
    'Show all items' -> num_items) used to raise NoMatchFound and 404 the page."""
    # item_count exceeds the items returned, so "Show all items" is rendered.
    mock_stub.GetTest.return_value = service_pb2.Test(
        id="t1", name="Detail", type=1, item_count=100, owner="user@example.com"
    )
    mock_stub.ListTestItems.return_value = iter(
        [service_pb2.TestItem(id="i1", question="Q1", answer="a")]
    )
    r = await client.get("/tests/t1")
    assert r.status_code == 200, r.text
    assert "/runs/create?test_id=t1" in r.text
    assert "/tests/t1?num_items=100" in r.text


@pytest.mark.asyncio
async def test_test_details_renders_item_table_and_instructions(client, mock_stub):
    mock_stub.GetTest.return_value = service_pb2.Test(
        id="t1",
        name="Detail",
        type=1,
        item_count=1,
        owner="user@example.com",
        instructions="Apply policy X.",
    )
    mock_stub.ListTestItems.return_value = iter(
        [
            service_pb2.TestItem(
                id="i1",
                question="Q1",
                context="ctx",
                choices=["yes", "no"],
                answer="yes",
                is_relevant=True,
            )
        ]
    )
    r = await client.get("/tests/t1")
    assert r.status_code == 200, r.text
    assert 'id="item-table"' in r.text
    assert 'data-filter-question="q1"' in r.text
    assert '<span class="choice-pill">no</span>' in r.text
    assert "Positive?" in r.text
    assert "Apply policy X." in r.text
    assert "eval-items-accordion" not in r.text


@pytest.mark.asyncio
async def test_test_details_survey_hides_eval_columns(client, mock_stub):
    mock_stub.GetTest.return_value = service_pb2.Test(
        id="t1", name="Survey", type=2, item_count=1, owner="user@example.com"
    )
    mock_stub.ListTestItems.return_value = iter(
        [service_pb2.TestItem(id="i1", question="Q1", choices=["a"])]
    )
    r = await client.get("/tests/t1")
    assert r.status_code == 200, r.text
    assert 'id="item-table"' in r.text
    assert "Positive?" not in r.text
    assert "Instructions</span>" not in r.text


@pytest.mark.asyncio
async def test_url_for_moves_non_path_kwargs_to_query(client):
    """Regression: FastAPI >= 0.141 nests included routes, which broke the old
    app.routes scan and made every query-kwarg url_for raise NoMatchFound."""
    from ui.server import templates

    url_for = templates.env.globals["url_for"]
    assert url_for("test_details", test_id="t1") == "/tests/t1"
    assert url_for("test_details", test_id="t1", num_items=5) == "/tests/t1?num_items=5"
    assert url_for("run_config", test_id="t1") == "/runs/create?test_id=t1"
    assert url_for("tests", show_all="true") == "/tests?show_all=true"
    assert url_for("static", filename="css/style.css") == "/static/css/style.css"


@pytest.mark.asyncio
async def test_import_controls_render(client):
    r = await client.get("/tests")
    assert r.status_code == 200, r.text
    assert 'onchange="importTestFile(this)"' in r.text

    r = await client.get("/tests/create")
    assert r.status_code == 200, r.text
    assert 'id="import-test-input"' in r.text


# ── auth ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_requires_auth(anon_client):
    r = await anon_client.post(
        "/tests/import",
        files={"file": ("t.csv", b"name,question\n", "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code in (303, 403)


# ── archive (.zip) import ───────────────────────────────────


@pytest.mark.asyncio
async def test_import_archive_roundtrip(client, mock_stub):
    _wire_create(mock_stub)
    manifest = {
        "name": "Zip Test",
        "description": "d",
        "instructions": "Apply policy X.",
        "type": "SURVEY",
        "provider": "acme",
        "labels": ["a", "b"],
        "items": [
            {"question": "Q1", "choices": ["x", "y"], "answer": "x"},
            {"question": "Q2", "choices": ["x", "y"], "answer": "y"},
        ],
    }
    r = await client.post(
        "/tests/import",
        files={"file": ("t.zip", _zip_bytes(manifest), "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["test_id"] == "imported-1"
    assert mock_stub.CreateTestItem.call_count == 2
    created_test = mock_stub.CreateTest.call_args.args[0]
    assert created_test.name == "Zip Test"
    assert created_test.type == 2  # SURVEY
    assert list(created_test.labels) == ["a", "b"]
    assert created_test.instructions == "Apply policy X."


@pytest.mark.asyncio
async def test_import_archive_legacy_test_items_key(client, mock_stub):
    """Manifests using the old ``test_items`` key still import."""
    _wire_create(mock_stub)
    manifest = {
        "name": "Legacy",
        "type": "EVALUATION",
        "test_items": [{"question": "Q1", "answer": "a"}],
    }
    r = await client.post(
        "/tests/import",
        files={"file": ("t.zip", _zip_bytes(manifest), "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateTestItem.call_count == 1


@pytest.mark.asyncio
async def test_import_archive_with_attachment(client, mock_stub):
    _wire_create(mock_stub)
    manifest = {
        "name": "With Attachment",
        "type": "EVALUATION",
        "items": [{"question": "Q1", "answer": "a", "attachment": "pic.png"}],
    }
    r = await client.post(
        "/tests/import",
        files={
            "file": (
                "t.zip",
                _zip_bytes(manifest, {"pic.png": b"\x89PNG..."}),
                "application/zip",
            )
        },
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateAttachment.call_count == 1
    created_item = mock_stub.CreateTestItem.call_args.args[0]
    assert created_item.attachment_id == "att-1"


@pytest.mark.asyncio
async def test_import_archive_missing_attachment(client, mock_stub):
    _wire_create(mock_stub)
    manifest = {
        "name": "Broken",
        "type": "EVALUATION",
        "items": [{"question": "Q1", "answer": "a", "attachment": "gone.png"}],
    }
    r = await client.post(
        "/tests/import",
        files={"file": ("t.zip", _zip_bytes(manifest), "application/zip")},
    )
    assert r.status_code == 400
    assert "gone.png" in r.json()["error"]


# ── CSV import (mirrors the CSV export format) ──────────────


@pytest.mark.asyncio
async def test_import_csv(client, mock_stub):
    """CSV export format: choices/labels are Python reprs from pandas.to_csv."""
    _wire_create(mock_stub)
    fields = [
        "name",
        "description",
        "instructions",
        "type",
        "provider",
        "labels",
        "response_id",
        "question",
        "context",
        "choices",
        "answer",
        "is_relevant",
    ]
    rows = [
        {
            "name": "CSV Test",
            "description": "desc",
            "instructions": "Apply policy X.",
            "type": "EVALUATION",
            "provider": "acme",
            "labels": "['toxicity', 'threats']",
            "response_id": "r1",
            "question": "Q one?",
            "context": "ctx1",
            "choices": "['Low', 'Medium', 'High']",
            "answer": "Low",
            "is_relevant": "False",
        },
        {
            "name": "CSV Test",
            "description": "desc",
            "instructions": "Apply policy X.",
            "type": "EVALUATION",
            "provider": "acme",
            "labels": "['toxicity', 'threats']",
            "response_id": "r2",
            "question": "Q two?",
            "context": "ctx2",
            "choices": "['Low', 'Medium', 'High']",
            "answer": "High",
            "is_relevant": "True",
        },
    ]
    r = await client.post(
        "/tests/import",
        files={"file": ("t.csv", _csv_bytes(rows, fields), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateTestItem.call_count == 2

    created_test = mock_stub.CreateTest.call_args.args[0]
    assert created_test.name == "CSV Test"
    assert created_test.type == 1  # EVALUATION
    assert list(created_test.labels) == ["toxicity", "threats"]
    assert created_test.instructions == "Apply policy X."

    item1 = mock_stub.CreateTestItem.call_args_list[0].args[0]
    assert item1.question == "Q one?"
    assert list(item1.choices) == ["Low", "Medium", "High"]
    assert item1.is_relevant is False
    item2 = mock_stub.CreateTestItem.call_args_list[1].args[0]
    assert item2.is_relevant is True


@pytest.mark.asyncio
async def test_import_csv_no_questions(client, mock_stub):
    _wire_create(mock_stub)
    rows = [{"name": "Empty", "question": "  "}]
    r = await client.post(
        "/tests/import",
        files={"file": ("t.csv", _csv_bytes(rows, ["name", "question"]), "text/csv")},
    )
    assert r.status_code == 400
    assert mock_stub.CreateTest.call_count == 0


# ── JSON import (records array and manifest dict) ───────────


@pytest.mark.asyncio
async def test_import_json_records(client, mock_stub):
    """JSON export format from pandas.to_json(orient='records')."""
    _wire_create(mock_stub)
    records = [
        {
            "name": "JSON Test",
            "type": "EVALUATION",
            "provider": "acme",
            "labels": ["l1"],
            "question": "Q1",
            "context": "c1",
            "choices": ["a", "b"],
            "answer": "a",
            "is_relevant": True,
        },
        {
            "name": "JSON Test",
            "type": "EVALUATION",
            "provider": "acme",
            "labels": ["l1"],
            "question": "Q2",
            "context": "c2",
            "choices": ["a", "b"],
            "answer": "b",
            "is_relevant": False,
        },
    ]
    r = await client.post(
        "/tests/import",
        files={"file": ("t.json", json.dumps(records).encode(), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateTestItem.call_count == 2
    item1 = mock_stub.CreateTestItem.call_args_list[0].args[0]
    assert list(item1.choices) == ["a", "b"]
    assert item1.is_relevant is True


@pytest.mark.asyncio
async def test_import_json_manifest_dict(client, mock_stub):
    _wire_create(mock_stub)
    manifest = {
        "name": "Dict Manifest",
        "type": "SURVEY",
        "items": [{"question": "Q1", "choices": ["a"], "answer": "a"}],
    }
    r = await client.post(
        "/tests/import",
        files={"file": ("t.json", json.dumps(manifest).encode(), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateTestItem.call_count == 1


# ── malformed uploads ───────────────────────────────────────


@pytest.mark.asyncio
async def test_import_unparseable(client, mock_stub):
    _wire_create(mock_stub)
    r = await client.post(
        "/tests/import",
        files={"file": ("t.json", b"{not valid json", "application/json")},
    )
    assert r.status_code == 400
    assert mock_stub.CreateTest.call_count == 0


# ── export regression (the KeyError bug) ────────────────────


@pytest.mark.asyncio
async def test_download_archive_roundtrip(client, mock_stub):
    """Guards the manifest ``items`` KeyError: export must produce a real zip."""
    mock_stub.GetTest.return_value = service_pb2.Test(
        id="test-1",
        name="Export Me",
        type=1,
        item_count=2,
        labels=["k"],
        instructions="Apply policy X.",
    )
    mock_stub.ListTestItems.return_value = iter(
        [
            service_pb2.TestItem(id="i1", question="Q1", answer="a"),
            service_pb2.TestItem(id="i2", question="Q2", answer="b"),
        ]
    )
    r = await client.get("/tests/test-1/download/archive")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["name"] == "Export Me"
    assert manifest["instructions"] == "Apply policy X."
    assert len(manifest["items"]) == 2
    assert manifest["items"][0]["question"] == "Q1"


# ── /tests/parse (create-page draft import; creates nothing) ─


@pytest.mark.asyncio
async def test_parse_requires_auth(anon_client):
    r = await anon_client.post(
        "/tests/parse",
        files={"file": ("t.csv", b"name,question\nx,y\n", "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code in (303, 403)


@pytest.mark.asyncio
async def test_parse_csv_returns_rows(client, mock_stub):
    fields = ["name", "type", "labels", "question", "choices", "answer", "is_relevant"]
    rows = [
        {
            "name": "CSV Test",
            "type": "EVALUATION",
            "labels": "['toxicity']",
            "question": "Q1",
            "choices": "['Low', 'High']",
            "answer": "Low",
            "is_relevant": "True",
        },
    ]
    r = await client.post(
        "/tests/parse",
        files={"file": ("t.csv", _csv_bytes(rows, fields), "text/csv")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "CSV Test"
    assert data["type"] == 1
    assert data["labels"] == ["toxicity"]
    assert len(data["items"]) == 1
    assert data["items"][0]["choices"] == ["Low", "High"]
    assert data["items"][0]["is_relevant"] is True
    # Parsing must not create anything.
    assert mock_stub.CreateTest.call_count == 0
    assert mock_stub.CreateTestItem.call_count == 0


@pytest.mark.asyncio
async def test_parse_json_records(client, mock_stub):
    records = [
        {
            "name": "J",
            "type": "SURVEY",
            "question": "Q1",
            "choices": ["a"],
            "answer": "a",
        }
    ]
    r = await client.post(
        "/tests/parse",
        files={"file": ("t.json", json.dumps(records).encode(), "application/json")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["type"] == 2  # SURVEY
    assert data["items"][0]["choices"] == ["a"]


@pytest.mark.asyncio
async def test_parse_zip_bundles_attachment(client, mock_stub):
    manifest = {
        "name": "Zip",
        "type": "EVALUATION",
        "items": [{"question": "Q1", "answer": "a", "attachment": "pic.png"}],
    }
    r = await client.post(
        "/tests/parse",
        files={
            "file": (
                "t.zip",
                _zip_bytes(manifest, {"pic.png": b"\x89PNGdata"}),
                "application/zip",
            )
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["items"][0]["attachment"] == "pic.png"
    assert len(data["attachments"]) == 1
    att = data["attachments"][0]
    assert att["filename"] == "pic.png"
    assert att["mime"] == "image/png"
    assert att["data_url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_parse_empty_csv(client, mock_stub):
    rows = [{"name": "n", "question": "  "}]
    r = await client.post(
        "/tests/parse",
        files={"file": ("t.csv", _csv_bytes(rows, ["name", "question"]), "text/csv")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_parse_unparseable(client, mock_stub):
    r = await client.post(
        "/tests/parse",
        files={"file": ("t.json", b"{bad json", "application/json")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_download_archive_roundtrips_through_import(client, mock_stub):
    """The archive we export must import cleanly (end-to-end round trip)."""
    mock_stub.GetTest.return_value = service_pb2.Test(
        id="test-1", name="RT", type=1, item_count=1, labels=["k"]
    )
    mock_stub.ListTestItems.return_value = iter(
        [service_pb2.TestItem(id="i1", question="Q1", choices=["a", "b"], answer="a")]
    )
    export = await client.get("/tests/test-1/download/archive")
    assert export.status_code == 200

    _wire_create(mock_stub)
    r = await client.post(
        "/tests/import",
        files={"file": ("rt.zip", export.content, "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert mock_stub.CreateTestItem.call_count == 1
    item = mock_stub.CreateTestItem.call_args.args[0]
    assert list(item.choices) == ["a", "b"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
