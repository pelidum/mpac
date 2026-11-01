import argparse
import base64
import io
import os
from pathlib import Path
import re
import time
import uuid
import zipfile

import grpc
import requests

from proto import service_pb2 as pb, service_pb2_grpc

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def main():
    parser = argparse.ArgumentParser(
        description="Exercise an already-applied on-prem stack using real TLS, UI uploads and an optional existing inference backend."
    )
    parser.add_argument("--url", default="https://localhost")
    parser.add_argument("--grpc", default="localhost:50051")
    parser.add_argument("--ca-cert", required=True)
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument(
        "--password-file",
        help="Read the password from a private file instead of MPAC_SMOKE_PASSWORD",
    )
    parser.add_argument(
        "--backend-url", help="Existing backend URL as registered in MPAC"
    )
    parser.add_argument("--model", help="Existing model ID or name")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    password = (
        Path(args.password_file).read_text().strip()
        if args.password_file
        else os.environ.get("MPAC_SMOKE_PASSWORD")
    )
    if not password:
        parser.error("Set MPAC_SMOKE_PASSWORD to the onboarding admin password")
    ca = str(Path(args.ca_cert).resolve())
    session = requests.Session()
    session.verify = ca
    login = session.post(
        args.url + "/login/password",
        data={"username": args.admin_email, "password": password},
        allow_redirects=False,
        timeout=30,
    )
    assert login.status_code == 303 and session.cookies.get("mpac_jwt"), (
        "UI password login failed"
    )
    print("UI password login: OK")
    page = session.get(args.url + "/tests/create", timeout=30)
    page.raise_for_status()
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert csrf, "UI did not issue a CSRF token"
    session.headers["X-CSRF-Token"] = csrf.group(1)
    metadata = (("x-jwt-token", session.cookies.get("mpac_jwt")),)
    credentials = grpc.ssl_channel_credentials(Path(ca).read_bytes())
    with grpc.secure_channel(args.grpc, credentials) as channel:
        grpc.channel_ready_future(channel).result(timeout=30)
        stub = service_pb2_grpc.MPACStub(channel)
        image = PNG + uuid.uuid4().bytes
        response = session.post(
            args.url + "/tests/create/submit",
            json={
                "name": "On-prem image smoke",
                "type": pb.EVALUATION,
                "attachments": [
                    {
                        "id": "pic",
                        "name": "pixel.png",
                        "mime": "image/png",
                        "file": base64.b64encode(image).decode(),
                    }
                ],
                "items": [
                    {
                        "question": "Is there an image?",
                        "choices": ["yes", "no"],
                        "answer": "yes",
                        "is_relevant": True,
                        "attachment_id": "pic",
                    }
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        test_id = response.json()["test_id"]
        items = list(
            stub.ListTestItems(
                pb.ListRequest(parent_id=test_id), metadata=metadata, timeout=30
            )
        )
        attachment_id = items[0].attachment_id
        attachment = stub.GetAttachment(
            pb.GetRequest(id=attachment_id), metadata=metadata, timeout=30
        )
        assert attachment.uri.startswith(args.url + "/blobs/") and not attachment.file
        downloaded = session.get(
            args.url + f"/attachments/{attachment_id}/file", timeout=30
        )
        downloaded.raise_for_status()
        assert downloaded.content == image, (
            "Signed download through Envoy changed the bytes"
        )
        archive = session.get(
            args.url + f"/tests/{test_id}/download/archive", timeout=60
        )
        archive.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
            assert zf.read("attachments/pixel.png") == image, (
                "UI private blob fetch failed"
            )
        print(f"UI upload, signed HTTPS download and ZIP export: OK (test {test_id})")

        if not args.backend_url:
            print(
                "Attachment smoke complete. Supply --backend-url and --model to test existing inference."
            )
            return
        if not args.model:
            parser.error("--model is required with --backend-url")
        backends = list(
            stub.ListBackends(pb.ListRequest(), metadata=metadata, timeout=30)
        )
        backend = next(
            (b for b in backends if b.base_url == args.backend_url and b.enabled), None
        )
        assert backend, f"No enabled backend registered for {args.backend_url}"
        backend_id = backend.id
        models = list(stub.ListModels(pb.ListRequest(), metadata=metadata, timeout=60))
        model = next(
            (
                m
                for m in models
                if m.backend_id == backend_id and args.model in (m.id, m.name)
            ),
            None,
        )
        assert model, f"Model {args.model} was not listed"
        reply = stub.CreateTest(
            pb.Test(name="On-prem inference smoke", type=pb.EVALUATION),
            metadata=metadata,
            timeout=30,
        )
        assert reply.code == pb.SUCCESS, reply.reason
        reply_item = stub.CreateTestItem(
            pb.TestItem(
                test_id=reply.id,
                question="Is two plus two equal to four? Answer yes or no.",
                choices=["yes", "no"],
                answer="yes",
                is_relevant=True,
            ),
            metadata=metadata,
            timeout=30,
        )
        assert reply_item.code == pb.SUCCESS, reply_item.reason
        started = list(
            stub.CreateTestRun(
                pb.TestRunRequest(test_id=reply.id, models=[model], sample_size=1),
                metadata=metadata,
                timeout=30,
            )
        )
        assert started and started[0].run_id
        run_id = started[0].run_id
        deadline = time.monotonic() + args.timeout
        while True:
            run = stub.GetTestRun(
                pb.GetRequest(id=run_id), metadata=metadata, timeout=30
            )
            if run.status != pb.IN_PROGRESS:
                break
            assert time.monotonic() < deadline, f"Run {run_id} did not finish"
            time.sleep(0.5)
        answers = list(
            stub.ListTestRunAnswers(
                pb.ListRequest(parent_id=run_id), metadata=metadata, timeout=30
            )
        )
        assert (
            run.status == pb.SUCCESS
            and len(answers) == 1
            and answers[0].status == pb.TestRunAnswer.OK
        ), "Inference failed"
        print(f"Inference: OK (run {run_id}, answer {answers[0].answer!r})")
        print("Smoke records remain in the UI for inspection.")


if __name__ == "__main__":
    main()
