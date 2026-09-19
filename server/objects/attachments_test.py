"""Unit tests for AttachmentsMixin: CreateAttachment validation and BatchGetAttachments
metadata_only flag."""

import asyncio
import unittest.mock as mock

import pytest

from server import service_pb2
from server.objects.attachments import AttachmentsMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Aborted(BaseException):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _make_context():
    ctx = mock.AsyncMock()

    async def abort(code, msg):
        raise _Aborted(code, msg)

    ctx.abort = abort
    return ctx


def _make_pool(rows=None):
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(return_value=rows or [])
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool, conn


class _StubAttachments(AttachmentsMixin):
    _owner = "user@example.com"
    db_pool = None

    async def get_request_owner(self, request):
        return self._owner

    async def is_user_admin(self, user_id):
        return True

    def attachment_parser(self, file_name, file_path, owner):
        # Returns UNSPECIFIED by default; individual tests can override.
        return service_pb2.FileAttachment(
            modality=service_pb2.FileModality.UNSPECIFIED_MODALITY
        )

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)


# ---------------------------------------------------------------------------
# CreateAttachment — early-return validation paths
# ---------------------------------------------------------------------------


class TestCreateAttachmentValidation:
    def test_missing_name_returns_error(self):
        stub = _StubAttachments()
        request = service_pb2.FileAttachment(file=b"data")
        result = _run(stub.CreateAttachment(request=request, context=_make_context()))
        assert result.code == service_pb2.ResponseCode.ERROR
        assert "name" in result.reason.lower()

    def test_missing_file_returns_error(self):
        stub = _StubAttachments()
        request = service_pb2.FileAttachment(name="test.jpg")
        result = _run(stub.CreateAttachment(request=request, context=_make_context()))
        assert result.code == service_pb2.ResponseCode.ERROR
        assert "file" in result.reason.lower()

    def test_size_check_uses_actual_bytes_not_claimed_size(self):
        """The size guard must check len(request.file), not request.file_size.
        A request with a small file but a huge file_size claim must pass the
        size guard (and fail later at modality check, not size)."""
        stub = _StubAttachments()
        request = service_pb2.FileAttachment(
            name="big.jpg",
            file=b"x",
            file_size=999_999_999_999,
        )
        result = _run(stub.CreateAttachment(request=request, context=_make_context()))
        assert result.code == service_pb2.ResponseCode.ERROR
        assert "size" not in result.reason.lower()

    def test_unsupported_modality_returns_error(self):
        """attachment_parser returning UNSPECIFIED_MODALITY causes an error reply."""
        stub = _StubAttachments()
        request = service_pb2.FileAttachment(name="test.bin", file=b"hello")
        result = _run(stub.CreateAttachment(request=request, context=_make_context()))
        assert result.code == service_pb2.ResponseCode.ERROR
        assert "unsupported" in result.reason.lower()

    def test_small_file_passes_size_guard(self):
        """A file within the size limit must pass the size guard and proceed to
        the modality check (which fails for unknown types, not size)."""
        stub = _StubAttachments()
        request = service_pb2.FileAttachment(
            name="ok.jpg",
            file=b"x" * 100,
        )
        result = _run(stub.CreateAttachment(request=request, context=_make_context()))
        assert "size" not in result.reason.lower()


# ---------------------------------------------------------------------------
# BatchGetAttachments — metadata_only flag
# ---------------------------------------------------------------------------


class TestBatchGetAttachmentsMetadataOnly:
    def _make_pool_with_attachment(self, attachment):
        pool, conn = _make_pool(rows=[{"proto_bytes": attachment.SerializeToString()}])
        return pool

    def test_metadata_only_clears_file_bytes(self):
        stub = _StubAttachments()
        attachment = service_pb2.FileAttachment(
            id="att-1",
            name="photo.jpg",
            file=b"binary data",
            modality=service_pb2.FileModality.IMAGE,
        )
        stub.db_pool = self._make_pool_with_attachment(attachment)

        async def run():
            results = []
            async for att in stub.BatchGetAttachments(
                request=service_pb2.BatchGetAttachmentsRequest(
                    ids=["att-1"], metadata_only=True
                ),
                context=_make_context(),
            ):
                results.append(att)
            return results

        results = _run(run())
        assert len(results) == 1
        assert results[0].name == "photo.jpg"
        assert results[0].file == b""

    def test_without_metadata_only_file_bytes_preserved(self):
        stub = _StubAttachments()
        attachment = service_pb2.FileAttachment(
            id="att-1",
            name="photo.jpg",
            file=b"binary data",
        )
        stub.db_pool = self._make_pool_with_attachment(attachment)

        async def run():
            results = []
            async for att in stub.BatchGetAttachments(
                request=service_pb2.BatchGetAttachmentsRequest(
                    ids=["att-1"], metadata_only=False
                ),
                context=_make_context(),
            ):
                results.append(att)
            return results

        results = _run(run())
        assert len(results) == 1
        assert results[0].file == b"binary data"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
