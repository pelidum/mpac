import io
from unittest import mock

import grpc
import httpx
import pytest

from proto import service_pb2
from ui import blobs
from ui.tests.conftest import RpcError


def _wire_upload(mock_stub):
    mock_stub.CreateAttachmentUpload.side_effect = None
    mock_stub.CreateAttachmentUpload.return_value = (
        service_pb2.CreateAttachmentUploadReply(
            attachment_id="upload-1",
            upload_uri="https://blobs.example/tmp/upload-1?sig",
            headers={"x-amz-meta-test": "1"},
        )
    )
    mock_stub.FinalizeAttachment.return_value = service_pb2.FileAttachment(id="sha-1")


def test_create_attachment_uploads_then_finalizes(mock_stub):
    _wire_upload(mock_stub)
    with mock.patch.object(blobs.requests, "put") as put:
        attachment_id = blobs.create_attachment(
            mock_stub, [("x-jwt-token", "t")], "pic.png", b"\x89PNG", "image/png"
        )

    assert attachment_id == "sha-1"
    request = mock_stub.CreateAttachmentUpload.call_args.args[0]
    assert (request.name, request.size_bytes, request.mime) == (
        "pic.png",
        4,
        "image/png",
    )
    put.assert_called_once_with(
        "https://blobs.example/tmp/upload-1?sig",
        data=b"\x89PNG",
        headers={"x-amz-meta-test": "1"},
        timeout=blobs.TRANSFER_TIMEOUT_S,
    )
    put.return_value.raise_for_status.assert_called_once()
    assert mock_stub.FinalizeAttachment.call_args.args[0].id == "upload-1"
    mock_stub.CreateAttachment.assert_not_called()


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.FAILED_PRECONDITION]
)
def test_create_attachment_falls_back_to_inline_bytes(mock_stub, code):
    mock_stub.CreateAttachmentUpload.side_effect = RpcError(code)
    mock_stub.CreateAttachment.return_value = service_pb2.StatusReply(id="sha-2")

    attachment_id = blobs.create_attachment(mock_stub, [], "a.txt", b"text")

    assert attachment_id == "sha-2"
    assert mock_stub.CreateAttachment.call_args.args[0].file == b"text"


def test_rejected_uploads_return_no_id_like_legacy_errors(mock_stub):
    _wire_upload(mock_stub)
    mock_stub.FinalizeAttachment.side_effect = RpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "Unsupported file type."
    )
    with mock.patch.object(blobs.requests, "put"):
        assert blobs.create_attachment(mock_stub, [], "x.bin", b"\x80") == ""


def test_other_upload_errors_propagate(mock_stub):
    mock_stub.CreateAttachmentUpload.side_effect = RpcError(
        grpc.StatusCode.PERMISSION_DENIED
    )
    with pytest.raises(grpc.RpcError):
        blobs.create_attachment(mock_stub, [], "a.txt", b"text")


def test_attachment_content_reads_inline_or_signed_uri():
    assert blobs.attachment_content(service_pb2.FileAttachment(file=b"inline")) == (
        b"inline"
    )
    with mock.patch.object(blobs.requests, "get") as get:
        get.return_value.content = b"remote"
        content = blobs.attachment_content(
            service_pb2.FileAttachment(uri="https://blobs.example/blobs/abc?sig")
        )
    assert content == b"remote"
    get.assert_called_once_with(
        "https://blobs.example/blobs/abc?sig", timeout=blobs.TRANSFER_TIMEOUT_S
    )


@pytest.mark.asyncio
async def test_attachment_file_redirects_to_the_signed_uri(client, mock_stub):
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment(
        id="abc", uri="https://blobs.example/blobs/abc?sig", mime="image/png"
    )

    r = await client.get("/attachments/abc/file", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "https://blobs.example/blobs/abc?sig"


@pytest.mark.asyncio
async def test_attachment_file_serves_legacy_inline_bytes(client, mock_stub):
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment(
        id="abc", file=b"\x89PNG", mime="image/png"
    )

    r = await client.get("/attachments/abc/file")

    assert r.status_code == 200
    assert r.content == b"\x89PNG"
    assert r.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_attachment_file_is_404_without_content(client, mock_stub):
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment()

    r = await client.get("/attachments/missing/file")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_stream_sends_chunks_with_the_declared_length_then_finalizes(
    mock_stub,
):
    _wire_upload(mock_stub)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["length"] = request.headers["content-length"]
        seen["body"] = request.read()
        return httpx.Response(200)

    async def chunks():
        yield b"ab"
        yield b"cd"

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with mock.patch.object(blobs, "_http_client", client):
        attachment_id = await blobs.upload_stream(
            mock_stub, [], "big.mp4", 4, chunks(), "video/mp4"
        )

    assert attachment_id == "sha-1"
    assert seen == {
        "url": "https://blobs.example/tmp/upload-1?sig",
        "length": "4",
        "body": b"abcd",
    }
    assert mock_stub.CreateAttachmentUpload.call_args.args[0].size_bytes == 4
    assert mock_stub.FinalizeAttachment.call_args.args[0].id == "upload-1"


@pytest.mark.asyncio
async def test_upload_route_returns_the_finalized_id(client, mock_stub):
    with mock.patch.object(
        blobs, "upload_stream", mock.AsyncMock(return_value="sha-9")
    ) as upload:
        r = await client.post(
            "/attachments/upload?name=a.png&mime=image/png", content=b"data"
        )

    assert r.status_code == 200
    assert r.json() == {"id": "sha-9"}
    assert upload.call_args.args[2:4] == ("a.png", 4)


@pytest.mark.asyncio
async def test_upload_route_rejects_files_over_the_limit(client, monkeypatch):
    monkeypatch.setenv("MPAC_MAX_ATTACHMENT_SIZE", "3")

    r = await client.post("/attachments/upload?name=a.txt", content=b"data")

    assert r.status_code == 413
    assert "exceeds maximum" in r.json()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.FAILED_PRECONDITION]
)
async def test_upload_route_asks_for_inline_without_blob_storage(
    client, mock_stub, code
):
    mock_stub.CreateAttachmentUpload.side_effect = RpcError(code)

    r = await client.post("/attachments/upload?name=a.txt", content=b"data")

    assert r.status_code == 501
    assert r.json() == {"inline": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, 400),
        (grpc.StatusCode.PERMISSION_DENIED, 403),
        (grpc.StatusCode.ALREADY_EXISTS, 409),
    ],
)
async def test_upload_route_reports_rejected_files(client, mock_stub, code, status):
    _wire_upload(mock_stub)
    mock_stub.FinalizeAttachment.side_effect = RpcError(code, "Rejected file")

    def transport():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )

    with mock.patch.object(blobs, "_http_client", transport):
        r = await client.post("/attachments/upload?name=a.bin", content=b"data")

    assert r.status_code == status
    assert r.json() == {"error": "Rejected file"}


@pytest.mark.asyncio
async def test_attachment_file_streams_and_forwards_ranges(
    client, mock_stub, monkeypatch
):
    monkeypatch.setenv("MPAC_BLOB_INTERNAL_ENDPOINT", "http://seaweedfs:8333")
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment(
        id="abc", uri="https://blobs.example/blobs/abc?sig", mime="video/mp4"
    )
    with mock.patch.object(blobs.requests, "get") as get:
        get.return_value.status_code = 206
        get.return_value.headers = {
            "Content-Length": "2",
            "Content-Range": "bytes 0-1/10",
        }
        get.return_value.iter_content.return_value = iter([b"ab"])
        r = await client.get("/attachments/abc/file", headers={"Range": "bytes=0-1"})

    assert r.status_code == 206
    assert r.content == b"ab"
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-range"] == "bytes 0-1/10"
    assert r.headers["accept-ranges"] == "bytes"
    assert get.call_args.kwargs["stream"] is True
    assert get.call_args.kwargs["headers"]["Range"] == "bytes=0-1"
    get.return_value.close.assert_called_once()


def test_copy_content_streams_blobs_and_writes_inline_bytes():
    destination = io.BytesIO()
    blobs.copy_content(service_pb2.FileAttachment(file=b"inline"), destination)
    assert destination.getvalue() == b"inline"

    destination = io.BytesIO()
    with mock.patch.object(blobs.requests, "get") as get:
        response = get.return_value.__enter__.return_value
        response.iter_content.return_value = iter([b"re", b"mote"])
        blobs.copy_content(
            service_pb2.FileAttachment(uri="https://blobs.example/blobs/abc?sig"),
            destination,
        )
    assert destination.getvalue() == b"remote"
    assert get.call_args.kwargs["stream"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_internal_blob_transport_preserves_signed_host_path_and_query(
    monkeypatch, mock_stub
):
    monkeypatch.setenv("MPAC_BLOB_INTERNAL_ENDPOINT", "http://seaweedfs:8333")
    monkeypatch.setenv("MPAC_BLOB_PUBLIC_ENDPOINT", "https://blobs.example")
    _wire_upload(mock_stub)
    with mock.patch.object(blobs.requests, "put") as put:
        assert blobs.create_attachment(mock_stub, [], "pic.png", b"data") == "sha-1"
    assert put.call_args.args == ("http://seaweedfs:8333/tmp/upload-1?sig",)
    assert put.call_args.kwargs["headers"] == {
        "Host": "blobs.example",
        "x-amz-meta-test": "1",
    }
    with mock.patch.object(blobs.requests, "get") as get:
        get.return_value.content = b"blob"
        assert (
            blobs.attachment_content(
                service_pb2.FileAttachment(
                    uri="https://blobs.example/blobs/sha?X-Amz-Signature=a%2Bb"
                )
            )
            == b"blob"
        )
    assert get.call_args.args == (
        "http://seaweedfs:8333/blobs/sha?X-Amz-Signature=a%2Bb",
    )
    assert get.call_args.kwargs["headers"] == {"Host": "blobs.example"}


def test_internal_blob_transport_rejects_unexpected_public_origin(monkeypatch):
    monkeypatch.setenv("MPAC_BLOB_INTERNAL_ENDPOINT", "http://seaweedfs:8333")
    monkeypatch.setenv("MPAC_BLOB_PUBLIC_ENDPOINT", "https://localhost")
    with pytest.raises(ValueError, match="does not match"):
        blobs._transfer_target("https://other.example/blobs/sha")


@pytest.mark.asyncio
async def test_attachment_preview_uses_internal_transport(
    client, mock_stub, monkeypatch
):
    monkeypatch.setenv("MPAC_BLOB_INTERNAL_ENDPOINT", "http://seaweedfs:8333")
    monkeypatch.setenv("MPAC_BLOB_PUBLIC_ENDPOINT", "https://localhost")
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment(
        id="abc",
        uri="https://localhost/blobs/blobs/abc?X-Amz-Signature=a%2Bb",
        mime="image/png",
    )
    with mock.patch.object(blobs.requests, "get") as get:
        get.return_value.status_code = 200
        get.return_value.iter_content.return_value = iter([b"\x89PNG"])
        get.return_value.headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = await client.get("/attachments/abc/file", follow_redirects=False)
    assert response.status_code == 200
    assert response.content == b"\x89PNG"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert "location" not in response.headers
    get.assert_called_once_with(
        "http://seaweedfs:8333/blobs/blobs/abc?X-Amz-Signature=a%2Bb",
        headers={"Host": "localhost"},
        timeout=blobs.TRANSFER_TIMEOUT_S,
        stream=True,
    )
    get.return_value.raise_for_status.assert_called_once()
    assert mock_stub.GetAttachment.call_args.kwargs["metadata"]


@pytest.mark.asyncio
async def test_attachment_preview_handles_blob_failure(client, mock_stub, monkeypatch):
    monkeypatch.setenv("MPAC_BLOB_INTERNAL_ENDPOINT", "http://seaweedfs:8333")
    mock_stub.GetAttachment.return_value = service_pb2.FileAttachment(
        id="abc", uri="https://localhost/blobs/blobs/abc?sig", mime="image/png"
    )
    with mock.patch.object(blobs.requests, "get") as get:
        get.return_value.raise_for_status.side_effect = blobs.requests.HTTPError(
            "unavailable"
        )
        response = await client.get("/attachments/abc/file", follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers
