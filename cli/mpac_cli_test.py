import http.server
import datetime
import json
import ssl
import urllib.error
import threading
from unittest import mock

import grpc
import pytest
from absl.testing import flagsaver
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from cli import mpac_cli as cli
from proto import service_pb2


class _Unimplemented(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


RECEIVED: list[tuple[str, bytes]] = []


class _UploadHandler(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        RECEIVED.append(
            (self.path, self.rfile.read(int(self.headers["Content-Length"])))
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def upload_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UploadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    RECEIVED.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _attachment():
    return service_pb2.FileAttachment(
        id="local-sha", name="clip.wav", file=b"RIFF....", mime="audio/x-wav"
    )


def test_upload_attachment_puts_bytes_then_finalizes(upload_server):
    service = mock.MagicMock()
    service.CreateAttachmentUpload.return_value = (
        service_pb2.CreateAttachmentUploadReply(
            attachment_id="upload-1", upload_uri=f"{upload_server}/tmp/upload-1"
        )
    )
    service.FinalizeAttachment.return_value = service_pb2.FileAttachment(id="sha")

    assert cli.upload_attachment(_attachment(), [], service) == "sha"

    assert RECEIVED == [("/tmp/upload-1", b"RIFF....")]
    request = service.CreateAttachmentUpload.call_args.args[0]
    assert (request.name, request.size_bytes) == ("clip.wav", 8)
    service.CreateAttachment.assert_not_called()


def test_upload_attachment_falls_back_to_inline_create():
    service = mock.MagicMock()
    service.CreateAttachmentUpload.side_effect = _Unimplemented()

    assert cli.upload_attachment(_attachment(), [], service) == "local-sha"

    assert service.CreateAttachment.call_args.kwargs["request"].file == b"RIFF...."


@pytest.fixture()
def https_upload_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UploadHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    RECEIVED.clear()
    try:
        yield f"https://localhost:{server.server_port}", cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_https_upload_trusts_explicit_certificate_but_keeps_verification(
    https_upload_server,
):
    endpoint, cert_path = https_upload_server
    service = mock.MagicMock()
    service.CreateAttachmentUpload.return_value = (
        service_pb2.CreateAttachmentUploadReply(
            attachment_id="upload-1", upload_uri=f"{endpoint}/tmp/upload-1"
        )
    )
    service.FinalizeAttachment.return_value = service_pb2.FileAttachment(id="sha")
    with flagsaver.flagsaver(mpac_cert_file=None):
        with pytest.raises(urllib.error.URLError) as error:
            cli.upload_attachment(_attachment(), [], service)
        assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
    service.FinalizeAttachment.assert_not_called()
    assert not RECEIVED
    with flagsaver.flagsaver(mpac_cert_file=str(cert_path)):
        assert cli.upload_attachment(_attachment(), [], service) == "sha"
        service.CreateAttachmentUpload.return_value.upload_uri = (
            endpoint.replace("localhost", "127.0.0.1") + "/tmp/upload-1"
        )
        with pytest.raises(urllib.error.URLError) as error:
            cli.upload_attachment(_attachment(), [], service)
        assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
    assert RECEIVED == [("/tmp/upload-1", b"RIFF....")]


def _bootstrap_files(tmp_path):
    items = tmp_path / "items"
    attachments = tmp_path / "attachments"
    items.mkdir()
    (attachments / "case").mkdir(parents=True)
    (attachments / "case/pic.png").write_bytes(b"image")
    (items / "case.json").write_text(
        json.dumps(
            [
                {
                    "test_id": "case",
                    "question": str(index),
                    "attachment_path": "pic.png",
                }
                for index in range(3)
            ]
        )
    )
    return items, attachments


def test_bootstrap_upload_failure_does_not_create_attachmentless_items(tmp_path):
    items, attachments = _bootstrap_files(tmp_path)
    service = mock.MagicMock()
    with (
        flagsaver.flagsaver(bootstrap_include=None),
        mock.patch.object(
            cli, "create_attachment", side_effect=ssl.SSLError("untrusted certificate")
        ) as upload,
    ):
        with pytest.raises(RuntimeError, match="Bootstrap failed for: case.json"):
            cli.bootstrap_test_items(
                str(items), str(attachments), {"case": "test-id"}, [], service
            )
    upload.assert_called_once()
    service.BatchCreateTestItems.assert_not_called()


def test_bootstrap_reuses_each_uploaded_attachment(tmp_path):
    items, attachments = _bootstrap_files(tmp_path)
    service = mock.MagicMock()
    service.BatchCreateTestItems.return_value = service_pb2.BatchCreateTestItemsReply(
        successful_count=3
    )
    with (
        flagsaver.flagsaver(bootstrap_include=None),
        mock.patch.object(
            cli, "create_attachment", return_value="attachment-id"
        ) as upload,
    ):
        cli.bootstrap_test_items(
            str(items), str(attachments), {"case": "test-id"}, [], service
        )
    upload.assert_called_once()
    batch = service.BatchCreateTestItems.call_args.args[0]
    assert len(batch.items) == 3
    assert {item.attachment_id for item in batch.items} == {"attachment-id"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
