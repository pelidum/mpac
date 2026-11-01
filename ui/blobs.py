import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import grpc
import httpx
import requests
from absl import logging

from proto import service_pb2

TRANSFER_TIMEOUT_S = 300
TRANSFER_CHUNK_BYTES = 1024 * 1024
MAX_INFERENCE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024 * 1024
_LEGACY_CODES = {grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.FAILED_PRECONDITION}


def _transfer_target(uri, headers=None):
    headers = dict(headers or {})
    internal = os.environ.get("MPAC_BLOB_INTERNAL_ENDPOINT", "")
    if not internal:
        return uri, headers
    endpoint, signed = urlsplit(internal), urlsplit(uri)
    public = os.environ.get("MPAC_BLOB_PUBLIC_ENDPOINT", "")
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("MPAC_BLOB_INTERNAL_ENDPOINT must be an HTTP(S) origin")
    if public and (signed.scheme, signed.netloc) != (
        urlsplit(public).scheme,
        urlsplit(public).netloc,
    ):
        raise ValueError("Signed blob URL does not match MPAC_BLOB_PUBLIC_ENDPOINT")
    headers["Host"] = signed.netloc
    return urlunsplit(
        (endpoint.scheme, endpoint.netloc, signed.path, signed.query, "")
    ), headers


def create_attachment(stub, metadata, name, content, mime=""):
    try:
        upload = stub.CreateAttachmentUpload(
            service_pb2.CreateAttachmentUploadRequest(
                name=name, size_bytes=len(content), mime=mime
            ),
            metadata=metadata,
        )
    except grpc.RpcError as error:
        if error.code() not in _LEGACY_CODES:
            raise
        return stub.CreateAttachment(
            service_pb2.FileAttachment(name=name, file=content, mime=mime),
            metadata=metadata,
        ).id
    target, headers = _transfer_target(upload.upload_uri, upload.headers)
    response = requests.put(
        target,
        data=content,
        headers=headers,
        timeout=TRANSFER_TIMEOUT_S,
    )
    response.raise_for_status()
    try:
        return stub.FinalizeAttachment(
            service_pb2.GetRequest(id=upload.attachment_id), metadata=metadata
        ).id
    except grpc.RpcError as error:
        if error.code() != grpc.StatusCode.INVALID_ARGUMENT:
            raise
        logging.warning(f"Attachment {name} rejected: {error.details()}")
        return ""


def max_attachment_bytes():
    return int(
        os.environ.get("MPAC_MAX_ATTACHMENT_SIZE") or DEFAULT_MAX_ATTACHMENT_BYTES
    )


def _http_client():
    return httpx.AsyncClient(timeout=httpx.Timeout(TRANSFER_TIMEOUT_S), trust_env=False)


async def upload_stream(stub, metadata, name, size, chunks, mime=""):
    loop = asyncio.get_running_loop()
    upload = await loop.run_in_executor(
        None,
        lambda: stub.CreateAttachmentUpload(
            service_pb2.CreateAttachmentUploadRequest(
                name=name, size_bytes=size, mime=mime
            ),
            metadata=metadata,
        ),
    )
    target, headers = _transfer_target(upload.upload_uri, upload.headers)
    headers["Content-Length"] = str(size)
    async with _http_client() as client:
        response = await client.put(target, content=chunks, headers=headers)
    response.raise_for_status()
    finalized = await loop.run_in_executor(
        None,
        lambda: stub.FinalizeAttachment(
            service_pb2.GetRequest(id=upload.attachment_id), metadata=metadata
        ),
    )
    return finalized.id


def open_content(attachment_pb, byte_range=None):
    target, headers = _transfer_target(attachment_pb.uri)
    if byte_range:
        headers["Range"] = byte_range
    response = requests.get(
        target, timeout=TRANSFER_TIMEOUT_S, stream=True, headers=headers
    )
    response.raise_for_status()
    return response


def copy_content(attachment_pb, destination):
    if not attachment_pb.uri:
        destination.write(attachment_pb.file)
        return
    with open_content(attachment_pb) as response:
        for chunk in response.iter_content(TRANSFER_CHUNK_BYTES):
            destination.write(chunk)


def attachment_content(attachment_pb):
    if not attachment_pb.uri:
        return attachment_pb.file
    target, headers = _transfer_target(attachment_pb.uri)
    response = requests.get(
        target, timeout=TRANSFER_TIMEOUT_S, **({"headers": headers} if headers else {})
    )
    response.raise_for_status()
    return response.content
