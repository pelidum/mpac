import asyncio
import json

import grpc
import harness
import pytest
from google.protobuf.json_format import MessageToDict

from proto import service_pb2, service_pb2_grpc

ADMIN = (("x-api-key", harness.ADMIN_API_KEY),)
ATTACHMENT_ID = "stored-in-blob-storage"


def _insert_blob_row(pg, db_name):
    attachment = service_pb2.FileAttachment(
        id=ATTACHMENT_ID,
        name="clip.txt",
        uri=f"blobs/{ATTACHMENT_ID}",
        upload_state=service_pb2.FileAttachment.READY,
    )
    document = MessageToDict(attachment, preserving_proto_field_name=True)

    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            await conn.execute(
                "INSERT INTO attachments (id, proto_jsonb, proto_bytes) "
                "VALUES ($1, $2::jsonb, $3)",
                ATTACHMENT_ID,
                json.dumps(document),
                attachment.SerializeToString(),
            )
        finally:
            await conn.close()

    asyncio.run(run())


def test_full_reads_fail_explicitly_without_blob_configuration():
    with (
        harness.postgres() as pg,
        harness.database(pg) as db_name,
        harness.server(
            pg,
            db_name,
            extra_env={"MPAC_BLOB_URL": ""},
        ) as address,
        grpc.insecure_channel(address) as channel,
    ):
        _insert_blob_row(pg, db_name)
        stub = service_pb2_grpc.MPACStub(channel)

        with pytest.raises(grpc.RpcError) as error:
            stub.GetAttachment(service_pb2.GetRequest(id=ATTACHMENT_ID), metadata=ADMIN)
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        with pytest.raises(grpc.RpcError) as error:
            list(
                stub.BatchGetAttachments(
                    service_pb2.BatchGetAttachmentsRequest(ids=[ATTACHMENT_ID]),
                    metadata=ADMIN,
                )
            )
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        metadata = list(
            stub.BatchGetAttachments(
                service_pb2.BatchGetAttachmentsRequest(
                    ids=[ATTACHMENT_ID], metadata_only=True
                ),
                metadata=ADMIN,
            )
        )
        assert [attachment.id for attachment in metadata] == [ATTACHMENT_ID]
        assert not metadata[0].uri


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
