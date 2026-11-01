import base64
import hashlib
import time
import urllib.error
import urllib.request

import filetype
import grpc
import pytest
from conftest import (
    BLOBS_ENABLED,
    MEMBER_ID,
    OUTSIDER_ID,
    attachment_content,
    create_test,
    rpc_code,
    wait_until,
)

from proto import service_pb2

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _create(stub, metadata, name, content):
    return stub.CreateAttachment(
        service_pb2.FileAttachment(name=name, file=content), metadata=metadata
    )


def test_text_attachment_lifecycle(stub, member):
    content = b"contract attachment text"
    reply = _create(stub, member, "notes.txt", content)
    assert reply.code == service_pb2.SUCCESS, reply.reason
    assert reply.id == hashlib.sha256(content).hexdigest()

    attachment = stub.GetAttachment(
        service_pb2.GetRequest(id=reply.id), metadata=member
    )
    assert attachment_content(attachment) == content
    assert attachment.modality == service_pb2.TEXT
    assert attachment.mime == "text/plain"
    assert attachment.extension == "txt"
    assert attachment.owner == MEMBER_ID
    assert attachment.file_size == 1
    assert attachment.visibility.type == service_pb2.Visibility.VISIBILITY_ORG_ONLY

    listed = {
        a.id: a
        for a in stub.ListAttachments(service_pb2.ListRequest(), metadata=member)
    }
    assert listed[reply.id].file == b""
    assert listed[reply.id].uri == ""
    assert listed[reply.id].name == "notes.txt"

    assert (
        stub.DeleteAttachment(
            service_pb2.DeleteRequest(id=reply.id), metadata=member
        ).code
        == service_pb2.SUCCESS
    )
    assert (
        stub.GetAttachment(service_pb2.GetRequest(id=reply.id), metadata=member).id
        == ""
    )


def test_image_attachment_is_sniffed(stub, member):
    reply = _create(stub, member, "pixel.bin", PNG_1X1)
    assert reply.code == service_pb2.SUCCESS, reply.reason

    attachment = stub.GetAttachment(
        service_pb2.GetRequest(id=reply.id), metadata=member
    )
    assert attachment.modality == service_pb2.IMAGE
    assert attachment.mime == "image/png"
    assert attachment.extension == "png"


def test_batch_get_honors_metadata_only(stub, member):
    first = _create(stub, member, "a.txt", b"batch a").id
    second = _create(stub, member, "b.txt", b"batch b").id
    request = service_pb2.BatchGetAttachmentsRequest(ids=[first, second, "missing"])

    full = {a.id: a for a in stub.BatchGetAttachments(request, metadata=member)}
    request.metadata_only = True
    metadata_only = {
        a.id: a for a in stub.BatchGetAttachments(request, metadata=member)
    }

    assert full.keys() == metadata_only.keys() == {first, second}
    assert attachment_content(full[first]) == b"batch a"
    assert metadata_only[first].file == b""
    assert metadata_only[first].uri == ""
    assert metadata_only[first].mime == "text/plain"


def test_create_attachment_validation(stub, member):
    duplicate_content = b"duplicate content"
    assert (
        _create(stub, member, "dup.txt", duplicate_content).code == service_pb2.SUCCESS
    )

    cases = {
        "missing name": _create(stub, member, "", b"x"),
        "missing data": _create(stub, member, "empty.txt", b""),
        "unsupported": _create(
            stub, member, "blob.bin", bytes([0x00, 0xFF, 0xFE, 0x80] * 8)
        ),
        "duplicate": _create(stub, member, "dup.txt", duplicate_content),
    }

    assert {name: reply.code for name, reply in cases.items()} == dict.fromkeys(
        cases, service_pb2.ERROR
    )
    assert cases["missing name"].reason == "Attachment name is required"
    assert cases["missing data"].reason == "File data is required"


def test_only_owner_or_admin_can_delete_attachments(stub, admin, member, outsider):
    attachment_id = _create(stub, member, "owned.txt", b"owned by member").id

    assert (
        stub.GetAttachment(
            service_pb2.GetRequest(id=attachment_id), metadata=outsider
        ).id
        == attachment_id
    )
    code, _ = rpc_code(
        stub.DeleteAttachment,
        service_pb2.DeleteRequest(id=attachment_id),
        metadata=outsider,
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED
    assert (
        stub.DeleteAttachment(
            service_pb2.DeleteRequest(id="missing"), metadata=admin
        ).code
        == service_pb2.ERROR
    )
    assert (
        stub.DeleteAttachment(
            service_pb2.DeleteRequest(id=attachment_id), metadata=admin
        ).code
        == service_pb2.SUCCESS
    )


def _runfile(path):
    with open(path, "rb") as f:
        return f.read()


SNIFFING_CORPUS = [
    ("pixel.png", PNG_1X1),
    ("photo.jpg", "cli/test_db/attachments/debug_animals/vision_case1.jpg"),
    ("clip.wav", "cli/test_db/attachments/debug_animals/hearing_case1.wav"),
    ("anim.gif", b"GIF89a\x01\x00\x01\x00\x80\x00\x00"),
    ("image.webp", b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00"),
    ("image.bmp", b"BM\x3a\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00\x28\x00"),
    ("image.tif", b"II*\x00\x08\x00\x00\x00"),
    ("song.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x21"),
    ("song.flac", b"fLaC\x00\x00\x00\x22"),
    ("song.ogg", b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00"),
    ("song.m4a", b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom"),
    ("movie.mp4", b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"),
    ("movie.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  "),
    ("movie.avi", b"RIFF\x24\x00\x00\x00AVI LIST"),
    ("movie.webm", b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm"),
    ("movie.mkv", b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska"),
    ("doc.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3"),
    ("archive.zip", b"PK\x03\x04\x14\x00\x00\x00"),
    ("notes.md", "# Notes ✓ ünïcødé".encode()),
    ("noext", b"plain text without extension"),
    ("binary.dat", bytes([0x80, 0x81, 0xFE, 0xFF] * 4)),
]


def _expected(name, content):
    kind = filetype.guess(content)
    if kind is None:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return None
        extension = name.rsplit(".", 1)[1] if "." in name else "txt"
        return service_pb2.TEXT, "text/plain", extension
    modality = {
        "audio": service_pb2.AUDIO,
        "image": service_pb2.IMAGE,
        "video": service_pb2.VIDEO,
    }.get(kind.mime.split("/")[0])
    if modality is None:
        return None
    return modality, kind.mime, kind.extension


@pytest.mark.parametrize(
    "name, source", SNIFFING_CORPUS, ids=[n for n, _ in SNIFFING_CORPUS]
)
def test_sniffing_matches_python_filetype(stub, member, name, source):
    content = _runfile(source) if isinstance(source, str) else source
    content += f" {name}".encode() if b"\x00" not in content[:1] else b""
    expected = _expected(name, content)

    reply = _create(stub, member, name, content)

    if expected is None:
        assert reply.code == service_pb2.ERROR
        assert reply.reason.startswith("Unsupported file type"), reply.reason
        return
    assert reply.code == service_pb2.SUCCESS, reply.reason
    assert reply.reason == f"Attachment '{name}' created successfully"
    attachment = stub.GetAttachment(
        service_pb2.GetRequest(id=reply.id), metadata=member
    )
    assert (attachment.modality, attachment.mime, attachment.extension) == expected
    assert attachment.file_size == (len(content) + 1023) // 1024


blob_only = pytest.mark.skipif(not BLOBS_ENABLED, reason="needs MPAC_CONTRACT_BLOBS=1")
inline_only = pytest.mark.skipif(BLOBS_ENABLED, reason="blob storage is configured")


def _upload(stub, metadata, name, content, **fields):
    reply = stub.CreateAttachmentUpload(
        service_pb2.CreateAttachmentUploadRequest(
            name=name, size_bytes=len(content), **fields
        ),
        metadata=metadata,
    )
    request = urllib.request.Request(
        reply.upload_uri, data=content, method="PUT", headers=dict(reply.headers)
    )
    urllib.request.urlopen(request, timeout=30).close()
    return reply


def _finalize(stub, metadata, attachment_id):
    return stub.FinalizeAttachment(
        service_pb2.GetRequest(id=attachment_id), metadata=metadata
    )


@blob_only
def test_upload_flow_stores_content_by_sha256(stub, member):
    content = PNG_1X1 + b" upload flow"
    reply = _upload(stub, member, "pixel.png", content)
    assert reply.attachment_id
    assert reply.expires_at.seconds > time.time()
    listed = [
        a.id for a in stub.ListAttachments(service_pb2.ListRequest(), metadata=member)
    ]
    assert reply.attachment_id not in listed

    attachment = _finalize(stub, member, reply.attachment_id)

    digest = hashlib.sha256(content).hexdigest()
    assert attachment.id == attachment.sha256 == digest
    assert attachment.upload_state == service_pb2.FileAttachment.READY
    assert (attachment.modality, attachment.mime, attachment.extension) == (
        service_pb2.IMAGE,
        "image/png",
        "png",
    )
    assert attachment.owner == MEMBER_ID
    assert attachment.file_size == 1
    assert attachment.visibility.type == service_pb2.Visibility.VISIBILITY_ORG_ONLY
    assert attachment_content(attachment) == content
    fetched = stub.GetAttachment(service_pb2.GetRequest(id=digest), metadata=member)
    assert attachment_content(fetched) == content
    listed = {
        a.id: a
        for a in stub.ListAttachments(service_pb2.ListRequest(), metadata=member)
    }
    assert listed[digest].uri == ""
    code, _ = rpc_code(_finalize, stub, member, reply.attachment_id)
    assert code == grpc.StatusCode.NOT_FOUND

    again = _finalize(
        stub, member, _upload(stub, member, "copy.png", content).attachment_id
    )
    assert again.id == digest

    assert (
        stub.DeleteAttachment(
            service_pb2.DeleteRequest(id=digest), metadata=member
        ).code
        == service_pb2.SUCCESS
    )
    with pytest.raises(urllib.error.HTTPError):
        attachment_content(fetched)


@blob_only
def test_upload_validation(stub, member, outsider):
    def code_of(call, *args):
        return rpc_code(call, *args, metadata=member)[0]

    assert (
        code_of(
            stub.CreateAttachmentUpload,
            service_pb2.CreateAttachmentUploadRequest(name="", size_bytes=1),
        )
        == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert (
        code_of(
            stub.CreateAttachmentUpload,
            service_pb2.CreateAttachmentUploadRequest(name="a.txt", size_bytes=0),
        )
        == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert (
        code_of(stub.FinalizeAttachment, service_pb2.GetRequest(id="missing"))
        == grpc.StatusCode.NOT_FOUND
    )

    not_uploaded = stub.CreateAttachmentUpload(
        service_pb2.CreateAttachmentUploadRequest(name="later.txt", size_bytes=4),
        metadata=member,
    ).attachment_id
    assert (
        code_of(stub.FinalizeAttachment, service_pb2.GetRequest(id=not_uploaded))
        == grpc.StatusCode.FAILED_PRECONDITION
    )

    owned = _upload(stub, member, "mine.txt", b"member upload").attachment_id
    code, _ = rpc_code(_finalize, stub, outsider, owned)
    assert code == grpc.StatusCode.PERMISSION_DENIED

    rejected = _upload(stub, member, "blob.bin", bytes([0x80, 0xFF] * 8)).attachment_id
    code, details = rpc_code(_finalize, stub, member, rejected)
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    assert details.startswith("Unsupported file type")
    code, _ = rpc_code(_finalize, stub, member, rejected)
    assert code == grpc.StatusCode.NOT_FOUND


@blob_only
def test_upload_honors_requested_visibility(stub, member, outsider):
    attachment = _finalize(
        stub,
        member,
        _upload(
            stub,
            member,
            "private.txt",
            b"owner only upload",
            visibility=service_pb2.Visibility(
                type=service_pb2.Visibility.VISIBILITY_OWNER_ONLY
            ),
        ).attachment_id,
    )

    assert (
        stub.GetAttachment(
            service_pb2.GetRequest(id=attachment.id), metadata=outsider
        ).id
        == ""
    )
    assert (
        stub.GetAttachment(service_pb2.GetRequest(id=attachment.id), metadata=member).id
        == attachment.id
    )
    assert OUTSIDER_ID != MEMBER_ID


@blob_only
def test_inline_create_is_capped_by_the_message_limit_with_blob_storage(stub, member):
    code, _ = rpc_code(_create, stub, member, "big.txt", b"x" * (9 * 1024 * 1024))
    assert code in {grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.OUT_OF_RANGE}


@blob_only
def test_runs_read_uploaded_attachments(stub, member, know_it_all_model):
    attachment = _finalize(
        stub,
        member,
        _upload(
            stub,
            member,
            "clip.wav",
            _runfile("cli/test_db/attachments/debug_animals/hearing_case1.wav"),
        ).attachment_id,
    )
    test_id = create_test(stub, member)
    stub.CreateTestItem(
        service_pb2.TestItem(
            test_id=test_id,
            question="What is this?",
            choices=["cat", "dog"],
            answer="cat",
            attachment_id=attachment.id,
        ),
        metadata=member,
    )
    run_id = next(
        iter(
            stub.CreateTestRun(
                service_pb2.TestRunRequest(
                    test_id=test_id, models=[know_it_all_model], sample_size=1
                ),
                metadata=member,
            )
        )
    ).run_id

    def answered():
        answers = list(
            stub.ListTestRunAnswers(
                service_pb2.ListRequest(parent_id=run_id), metadata=member
            )
        )
        return answers if answers else None

    answer = wait_until(answered)[0]
    assert answer.status == service_pb2.TestRunAnswer.OK
    assert answer.attachment_type == service_pb2.AUDIO


@inline_only
def test_upload_rpcs_need_blob_storage(stub, member):
    for call, request in [
        (
            stub.CreateAttachmentUpload,
            service_pb2.CreateAttachmentUploadRequest(name="a.txt", size_bytes=1),
        ),
        (stub.FinalizeAttachment, service_pb2.GetRequest(id="anything")),
    ]:
        code, _ = rpc_code(call, request, metadata=member)
        assert code in {
            grpc.StatusCode.UNIMPLEMENTED,
            grpc.StatusCode.FAILED_PRECONDITION,
        }


@blob_only
def test_duplicate_upload_does_not_disclose_private_attachment(stub, member, outsider):
    content = b"private duplicate regression"
    original = _finalize(
        stub,
        member,
        _upload(
            stub,
            member,
            "private-project.txt",
            content,
            visibility=service_pb2.Visibility(
                type=service_pb2.Visibility.VISIBILITY_OWNER_ONLY
            ),
        ).attachment_id,
    )
    assert not stub.GetAttachment(
        service_pb2.GetRequest(id=original.id), metadata=outsider
    ).id
    duplicate = _upload(stub, outsider, "my-file.txt", content)
    code, _ = rpc_code(
        stub.FinalizeAttachment,
        service_pb2.GetRequest(id=duplicate.attachment_id),
        metadata=outsider,
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED
    assert not stub.GetAttachment(
        service_pb2.GetRequest(id=original.id), metadata=outsider
    ).id


@blob_only
def test_duplicate_upload_preserves_requested_visibility(stub, member):
    content = b"visibility conflict regression"
    _finalize(stub, member, _upload(stub, member, "shared.txt", content).attachment_id)
    private = _upload(
        stub,
        member,
        "private.txt",
        content,
        visibility=service_pb2.Visibility(
            type=service_pb2.Visibility.VISIBILITY_OWNER_ONLY
        ),
    )
    code, _ = rpc_code(
        stub.FinalizeAttachment,
        service_pb2.GetRequest(id=private.attachment_id),
        metadata=member,
    )
    assert code == grpc.StatusCode.ALREADY_EXISTS


@blob_only
def test_pending_upload_does_not_consume_list_limit(stub, member):
    _finalize(
        stub,
        member,
        _upload(stub, member, "ready.txt", b"list limit regression").attachment_id,
    )
    stub.CreateAttachmentUpload(
        service_pb2.CreateAttachmentUploadRequest(name="pending.txt", size_bytes=10),
        metadata=member,
    )
    listed = list(
        stub.ListAttachments(service_pb2.ListRequest(limit=1), metadata=member)
    )
    assert len(listed) == 1
    assert listed[0].upload_state != service_pb2.FileAttachment.PENDING


@blob_only
def test_delete_test_removes_exclusive_blob(stub, member):
    attachment = _finalize(
        stub,
        member,
        _upload(
            stub, member, "exclusive.txt", b"exclusive blob deletion regression"
        ).attachment_id,
    )
    test_id = create_test(stub, member)
    reply = stub.CreateTestItem(
        service_pb2.TestItem(
            test_id=test_id,
            question="Check cleanup?",
            choices=["yes", "no"],
            answer="yes",
            attachment_id=attachment.id,
        ),
        metadata=member,
    )
    assert reply.code == service_pb2.SUCCESS, reply.reason
    assert (
        stub.DeleteTest(service_pb2.DeleteRequest(id=test_id), metadata=member).code
        == service_pb2.SUCCESS
    )
    assert not stub.GetAttachment(
        service_pb2.GetRequest(id=attachment.id), metadata=member
    ).id
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(attachment.uri, timeout=30)
    assert error.value.code == 404


@blob_only
def test_upload_replacement_and_delete_are_serialized(blob_store, admin):
    import concurrent.futures
    import http.client
    import http.server
    import threading
    from urllib.parse import unquote, urlsplit

    import harness
    from proto import service_pb2_grpc

    upstream = urlsplit(blob_store.endpoint)
    replacement = bytes([0, 255, 254, 128]) * 32
    copying = threading.Event()
    release = threading.Event()
    state = {}
    inspecting = threading.Event()
    resume_inspection = threading.Event()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            deleting_blob = (
                self.command == "DELETE" and "/blobs/" in unquote(self.path)
            ) or (
                self.command == "POST" and "delete" in self.path and b"blobs/" in body
            )
            if deleting_blob and state.get("fail_delete"):
                state["failed_deletes"] = state.get("failed_deletes", 0) + 1
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if (
                self.command == "GET"
                and "/staging/" in self.path
                and state.get("pause_inspect")
            ):
                inspecting.set()
                assert resume_inspection.wait(timeout=20), (
                    "test did not release inspection"
                )
            source = self.headers.get("x-amz-copy-source", "")
            if "staging/" in unquote(source):
                copying.set()
                assert release.wait(timeout=20), "test did not release publication"
                request = urllib.request.Request(
                    state["upload"], data=replacement, method="PUT"
                )
                urllib.request.urlopen(request, timeout=30).close()
            connection = http.client.HTTPConnection(
                upstream.hostname, upstream.port, timeout=30
            )
            connection.request(
                self.command, self.path, body=body, headers=dict(self.headers)
            )
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.read())
            connection.close()

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = forward

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    env = blob_store.env()
    env["AWS_ENDPOINT"] = f"http://127.0.0.1:{proxy.server_port}"
    env["MPAC_MAX_ATTACHMENT_SIZE"] = "32"
    try:
        with (
            harness.postgres() as pg,
            harness.database(pg) as db,
            harness.server(pg, db, extra_env=env) as address,
        ):
            with (
                grpc.insecure_channel(address) as channel,
                concurrent.futures.ThreadPoolExecutor() as executor,
            ):
                local = service_pb2_grpc.MPACStub(channel)
                upload = _upload(local, admin, "safe.txt", b"safe text")
                state["upload"] = upload.upload_uri
                finalizing = executor.submit(
                    _finalize, local, admin, upload.attachment_id
                )
                assert copying.wait(timeout=20), (
                    "publication never reached staging copy"
                )
                release.set()
                attachment = finalizing.result(timeout=20)
                with urllib.request.urlopen(attachment.uri, timeout=30) as response:
                    content = response.read()
                assert content == b"safe text"
                assert hashlib.sha256(content).hexdigest() == attachment.sha256

                # A new publication holds the same lock as blob deletion.
                copying.clear()
                release.clear()
                second = _upload(local, admin, "safe.txt", b"safe text")
                state["upload"] = second.upload_uri
                finalizing = executor.submit(
                    _finalize, local, admin, second.attachment_id
                )
                assert copying.wait(timeout=20)
                deleting = executor.submit(
                    local.DeleteAttachment,
                    service_pb2.DeleteRequest(id=attachment.id),
                    metadata=admin,
                )
                try:
                    deleting.result(timeout=0.2)
                    pytest.fail(
                        "deletion passed a publication holding the lifecycle lock"
                    )
                except concurrent.futures.TimeoutError:
                    pass
                release.set()
                finalizing.result(timeout=20)
                assert deleting.result(timeout=20).code == service_pb2.SUCCESS
                assert not local.GetAttachment(
                    service_pb2.GetRequest(id=attachment.id), metadata=admin
                ).id
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(attachment.uri, timeout=30)
                assert error.value.code == 404

                # A failed object delete remains durable and is retried independently.
                retry = _finalize(
                    local,
                    admin,
                    _upload(local, admin, "safe.txt", b"safe text").attachment_id,
                )
                state["fail_delete"] = True
                assert (
                    local.DeleteAttachment(
                        service_pb2.DeleteRequest(id=retry.id), metadata=admin
                    ).code
                    == service_pb2.SUCCESS
                )
                assert state.get("failed_deletes", 0) > 0, (
                    "delete failure injection was not exercised"
                )
                with urllib.request.urlopen(retry.uri, timeout=30) as response:
                    assert response.read() == b"safe text"
                state["fail_delete"] = False
                with pytest.raises(grpc.RpcError) as error:
                    _finalize(local, admin, "missing-upload")
                assert error.value.code() == grpc.StatusCode.NOT_FOUND
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(retry.uri, timeout=30)
                assert error.value.code == 404

                # Deletion during inspection must prevent publication entirely.
                cancelled = _upload(local, admin, "cancelled.txt", b"cancelled upload")
                state["pause_inspect"] = True
                finalizing = executor.submit(
                    _finalize, local, admin, cancelled.attachment_id
                )
                assert inspecting.wait(timeout=20)
                assert (
                    local.DeleteAttachment(
                        service_pb2.DeleteRequest(id=cancelled.attachment_id),
                        metadata=admin,
                    ).code
                    == service_pb2.SUCCESS
                )
                resume_inspection.set()
                with pytest.raises(grpc.RpcError) as error:
                    finalizing.result(timeout=20)
                assert error.value.code() == grpc.StatusCode.NOT_FOUND
                assert not local.GetAttachment(
                    service_pb2.GetRequest(
                        id=hashlib.sha256(b"cancelled upload").hexdigest()
                    ),
                    metadata=admin,
                ).id
    finally:
        release.set()
        resume_inspection.set()
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)
