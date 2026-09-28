import ast
import base64
import csv
import io
import json
import os
import traceback
import zipfile

import grpc
import pandas as pd

from absl import logging
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()


@router.get("/tests")
async def tests(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    show_all = request.query_params.get("show_all", "false").lower() == "true"
    limit = 10000 if show_all else 500
    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()

    test_pbs = await loop.run_in_executor(
        None,
        lambda: list(
            stub.ListTests(service_pb2.ListRequest(limit=limit), metadata=metadata)
        ),
    )
    return templates.TemplateResponse(
        request,
        "tests.html",
        {
            "page_title": "Tests",
            "test_pbs": test_pbs,
            "is_truncated": not show_all and len(test_pbs) >= 500,
            "total_shown": len(test_pbs),
            "is_admin": user.is_admin,
            "user_email": user.email,
        },
    )


@router.get("/tests/create")
async def create_test(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    from ui.server import templates

    provider = user.org_domain or ""
    if not provider and "@" in user.email:
        provider = user.email.split("@", 1)[1]
    return templates.TemplateResponse(
        request,
        "create_test.html",
        {
            "page_title": "Pelidum MPAC - Create Test",
            "default_provider": provider,
            "is_admin": user.is_admin,
            "edit_mode": False,
        },
    )


@router.post("/tests/create/submit")
async def submit_test(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        data = await request.json()
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = service_pb2.Test(
            name=data.get("name"),
            description=data.get("description"),
            type=data.get("type"),
            item_count=len(data.get("items", [])),
            labels=data.get("labels", []),
            provider=data.get("provider", ""),
        )
        test_create_response = await loop.run_in_executor(
            None, lambda: stub.CreateTest(test_pb, metadata=metadata)
        )
        if not test_create_response.id and test_create_response.code == 1:
            raise ValueError("Failed to write initial test metadata")

        attachments_map = {}
        for attachment_data in data.get("attachments", []):
            file_bytes = base64.b64decode(attachment_data.get("file"))
            attachment_pb = service_pb2.FileAttachment(
                name=attachment_data.get("name"),
                extension=attachment_data.get("extension"),
                modality=attachment_data.get("modality"),
                file=file_bytes,
                file_size=attachment_data.get("file_size"),
                mime=attachment_data.get("mime"),
            )
            attachment_response = await loop.run_in_executor(
                None,
                lambda pb=attachment_pb: stub.CreateAttachment(pb, metadata=metadata),
            )
            attachments_map[attachment_data.get("id")] = attachment_response.id

        for item_data in data.get("items", []):
            attachment_preid = item_data.get("attachment_id")
            attachment_id = (
                attachments_map.get(attachment_preid, "") if attachment_preid else ""
            )
            item_pb = service_pb2.TestItem(
                test_id=test_create_response.id,
                question=item_data.get("question"),
                context=item_data.get("context"),
                choices=item_data.get("choices"),
                answer=item_data.get("answer"),
                is_relevant=item_data.get("is_relevant"),
                attachment_id=attachment_id,
            )
            await loop.run_in_executor(
                None, lambda pb=item_pb: stub.CreateTestItem(pb, metadata=metadata)
            )

        return JSONResponse(
            {
                "status": "ok",
                "test_id": test_create_response.id,
                "message": f"Test '{data.get('name')}' created with {len(data.get('items', []))} items",
            }
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Unable to create test"}, status_code=500
        )


@router.get("/tests/{test_id}/download/csv")
async def download_test_csv(
    test_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )
        test_dict = MessageToDict(
            test_pb,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
        )
        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )
        rows = []
        for item_pb in test_item_pbs:
            row = test_dict.copy()
            row.update(
                {
                    "response_id": item_pb.id,
                    "question": item_pb.question,
                    "context": item_pb.context,
                    "choices": list(item_pb.choices),
                    "answer": item_pb.answer,
                    "is_relevant": item_pb.is_relevant,
                }
            )
            rows.append(row)
        csv_data = pd.DataFrame(rows).to_csv(index=False)
        return StreamingResponse(
            iter([csv_data]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=mpac_test_{test_id}.csv"
            },
        )
    except Exception as e:
        logging.error(f"CSV download failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/tests/{test_id}/download/json")
async def download_test_json(
    test_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )
        test_dict = MessageToDict(
            test_pb,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
        )
        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )
        rows = []
        for item_pb in test_item_pbs:
            row = test_dict.copy()
            row.update(
                {
                    "response_id": item_pb.id,
                    "question": item_pb.question,
                    "context": item_pb.context,
                    "choices": list(item_pb.choices),
                    "answer": item_pb.answer,
                    "is_relevant": item_pb.is_relevant,
                }
            )
            rows.append(row)
        json_data = pd.DataFrame(rows).to_json(orient="records")
        return StreamingResponse(
            iter([json_data]),
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=mpac_test_{test_id}.json"
            },
        )
    except Exception as e:
        logging.error(f"JSON download failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/tests/{test_id}/download/archive")
async def download_test_archive(
    test_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )
        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )

        attachment_ids = list(
            {item.attachment_id for item in test_item_pbs if item.attachment_id}
        )
        attachments_by_id = {}
        if attachment_ids:
            attachment_pbs = await loop.run_in_executor(
                None,
                lambda: list(
                    stub.BatchGetAttachments(
                        service_pb2.BatchGetAttachmentsRequest(
                            ids=attachment_ids, metadata_only=False
                        ),
                        metadata=metadata,
                    )
                ),
            )
            attachments_by_id = {a.id: a for a in attachment_pbs}

        id_to_filename = {}
        used_filenames = set()
        for att_id, att in attachments_by_id.items():
            name = att.name or f"attachment.{att.extension or 'bin'}"
            base, ext = os.path.splitext(name)
            unique_name = name
            counter = 2
            while unique_name in used_filenames:
                unique_name = f"{base}_{counter}{ext}"
                counter += 1
            used_filenames.add(unique_name)
            id_to_filename[att_id] = unique_name

        type_name = "EVALUATION" if test_pb.type == 1 else "SURVEY"
        manifest = {
            "name": test_pb.name,
            "description": test_pb.description,
            "type": type_name,
            "provider": test_pb.provider,
            "labels": list(test_pb.labels),
            "items": [],
        }
        for item_pb in test_item_pbs:
            item_dict = {
                "question": item_pb.question,
                "context": item_pb.context,
                "choices": list(item_pb.choices),
                "answer": item_pb.answer,
                "is_relevant": item_pb.is_relevant,
            }
            if item_pb.attachment_id and item_pb.attachment_id in id_to_filename:
                item_dict["attachment"] = id_to_filename[item_pb.attachment_id]
            manifest["items"].append(item_dict)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for att_id, filename in id_to_filename.items():
                att = attachments_by_id[att_id]
                zf.writestr(
                    f"attachments/{filename}",
                    att.file,
                    compress_type=zipfile.ZIP_STORED,
                )
        buf.seek(0)

        safe_name = "".join(
            c if c.isalnum() or c in "._- " else "_" for c in test_pb.name
        ).strip()[:80]
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
        )
    except Exception as e:
        logging.error(f"Archive download failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)


TEST_TYPE_MAP = {"EVALUATION": 1, "SURVEY": 2}

# Caps for uploaded test exports. The item cap must fit the largest real tests
# (~52k items); the byte-size caps are what bound memory.
MAX_IMPORT_ITEMS = 100_000
MAX_IMPORT_SIZE = 100 * 1024 * 1024  # 100 MB upload
MAX_DECOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB uncompressed archive


class _ImportFileError(Exception):
    """A user-facing failure while parsing an uploaded test export."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _manifest_items(manifest: dict) -> list:
    """Return a manifest's item list, tolerating the legacy ``test_items`` key."""
    items = manifest.get("items")
    if items is None:
        items = manifest.get("test_items")
    return items or []


def _coerce_str_list(value) -> list:
    """Normalize a ``choices``/``labels`` cell to a list of strings.

    JSON exports carry real arrays; CSV exports carry a Python ``repr`` such as
    ``"['Low', 'Medium', 'High']"`` (produced by ``pandas.to_csv``), so fall back
    to JSON then to ``ast.literal_eval`` before treating it as a lone value.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    return [text]


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _manifest_from_rows(rows: list) -> dict:
    """Build a manifest from flattened CSV/JSON export rows.

    Each exported row repeats the test-level columns and carries one item, so
    test metadata is read from the first row; the rows themselves are the items
    (``_normalize_items`` picks out the item fields).
    """
    first = rows[0]
    return {
        "name": str(first.get("name") or ""),
        "description": str(first.get("description") or ""),
        "type": str(first.get("type") or "EVALUATION"),
        "provider": str(first.get("provider") or ""),
        "labels": first.get("labels"),
        "items": rows,
    }


def _archive_attachment_names(zf: zipfile.ZipFile) -> set:
    return {
        n.removeprefix("attachments/")
        for n in zf.namelist()
        if n.startswith("attachments/") and not n.endswith("/")
    }


def _parse_import_file(content: bytes, filename: str):
    """Parse an uploaded test export into ``(manifest, zip_or_None)``.

    Shared by ``/tests/import`` (creates the test) and ``/tests/parse`` (fills the
    create-page draft table) so both read CSV, JSON, and ZIP exports identically.
    A ZIP archive carries manifest.json plus attachment files; CSV/JSON exports
    are flattened (one row per item), carry no attachments, and return no zip.
    Raises :class:`_ImportFileError` on failure.
    """
    filename = (filename or "").lower()

    if zipfile.is_zipfile(io.BytesIO(content)):
        zf = zipfile.ZipFile(io.BytesIO(content))
        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > MAX_DECOMPRESSED_SIZE:
            raise _ImportFileError("Decompressed archive exceeds size limit", 413)
        if "manifest.json" not in zf.namelist():
            raise _ImportFileError("Archive missing manifest.json")
        return json.loads(zf.read("manifest.json")), zf

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _ImportFileError(
            "Unsupported file. Upload a .zip, .csv, or .json export."
        )

    is_json = filename.endswith(".json") or (
        not filename.endswith(".csv") and text.lstrip()[:1] in ("[", "{")
    )
    try:
        if is_json:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed, None
            if isinstance(parsed, list) and parsed:
                return _manifest_from_rows(parsed), None
            raise _ImportFileError("No test found in JSON file")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise _ImportFileError("No rows found in CSV export")
        return _manifest_from_rows(rows), None
    except (json.JSONDecodeError, csv.Error) as e:
        raise _ImportFileError(f"Could not parse file: {e}")


def _normalize_items(manifest: dict) -> list:
    """Return the manifest's items with every field coerced to its proto type."""
    return [
        {
            "question": str(i.get("question") or ""),
            "context": str(i.get("context") or ""),
            "choices": _coerce_str_list(i.get("choices")),
            "answer": str(i.get("answer") or ""),
            "is_relevant": _coerce_bool(i.get("is_relevant", False)),
            "attachment": i.get("attachment") or None,
        }
        for i in _manifest_items(manifest)
    ]


@router.post("/tests/import")
async def import_test(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
    file: UploadFile = File(...),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        content = await file.read()
        if len(content) > MAX_IMPORT_SIZE:
            return JSONResponse(
                {"error": f"Upload exceeds {MAX_IMPORT_SIZE // (1024 * 1024)}MB limit"},
                status_code=413,
            )

        try:
            manifest, zf = _parse_import_file(content, file.filename)
        except _ImportFileError as e:
            return JSONResponse({"error": e.message}, status_code=e.status_code)

        name = (manifest.get("name") or "").strip()
        if not name:
            return JSONResponse(
                {"error": "Test name is required in manifest"}, status_code=400
            )
        valid_items = [i for i in _normalize_items(manifest) if i["question"].strip()]
        if not valid_items:
            return JSONResponse(
                {"error": "At least one item with a question is required"},
                status_code=400,
            )
        if len(valid_items) > MAX_IMPORT_ITEMS:
            return JSONResponse(
                {"error": f"Import exceeds maximum of {MAX_IMPORT_ITEMS} items"},
                status_code=400,
            )

        # Only ZIP archives carry attachment files.
        referenced_files = set()
        if zf is not None:
            referenced_files = {i["attachment"] for i in valid_items if i["attachment"]}
            missing = referenced_files - _archive_attachment_names(zf)
            if missing:
                return JSONResponse(
                    {
                        "error": f"Missing attachment files: {', '.join(sorted(missing))}"
                    },
                    status_code=400,
                )

        test_type = TEST_TYPE_MAP.get((manifest.get("type") or "EVALUATION").upper(), 1)
        test_pb = service_pb2.Test(
            name=name,
            description=manifest.get("description") or "",
            type=test_type,
            item_count=len(valid_items),
            labels=_coerce_str_list(manifest.get("labels")),
            provider=manifest.get("provider") or "",
        )
        test_response = await loop.run_in_executor(
            None, lambda: stub.CreateTest(test_pb, metadata=metadata)
        )
        if not test_response.id:
            raise ValueError("Failed to create test")

        attachment_map = {}
        for filename in referenced_files:
            file_bytes = zf.read(f"attachments/{filename}")
            ext = os.path.splitext(filename)[1].lstrip(".")
            mime = _guess_mime(ext)
            modality = _mime_to_modality(mime)
            att_pb = service_pb2.FileAttachment(
                name=filename,
                extension=ext,
                modality=modality,
                file=file_bytes,
                file_size=len(file_bytes),
                mime=mime,
            )
            att_response = await loop.run_in_executor(
                None,
                lambda pb=att_pb: stub.CreateAttachment(pb, metadata=metadata),
            )
            attachment_map[filename] = att_response.id

        for item_data in valid_items:
            att_filename = item_data["attachment"]
            attachment_id = attachment_map.get(att_filename, "") if att_filename else ""
            item_pb = service_pb2.TestItem(
                test_id=test_response.id,
                question=item_data["question"],
                context=item_data["context"],
                choices=item_data["choices"],
                answer=item_data["answer"],
                is_relevant=item_data["is_relevant"],
                attachment_id=attachment_id,
            )
            await loop.run_in_executor(
                None,
                lambda pb=item_pb: stub.CreateTestItem(pb, metadata=metadata),
            )

        return JSONResponse(
            {
                "status": "ok",
                "test_id": test_response.id,
                "message": f"Imported '{name}' with {len(valid_items)} items",
            }
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return JSONResponse({"error": f"Import failed: {e}"}, status_code=500)


@router.post("/tests/parse")
async def parse_test_import(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
    file: UploadFile = File(...),
):
    """Parse a CSV/JSON/ZIP export and return its contents for the draft table.

    Unlike ``/tests/import`` this creates nothing: it hands the create page the
    parsed metadata, items, and (for archives) attachment bytes so the user can
    review and edit before saving. Both routes share :func:`_parse_import_file`.
    """
    try:
        content = await file.read()
        if len(content) > MAX_IMPORT_SIZE:
            return JSONResponse(
                {"error": f"Upload exceeds {MAX_IMPORT_SIZE // (1024 * 1024)}MB limit"},
                status_code=413,
            )

        try:
            manifest, zf = _parse_import_file(content, file.filename)
        except _ImportFileError as e:
            return JSONResponse({"error": e.message}, status_code=e.status_code)

        items = _normalize_items(manifest)
        if not any(i["question"].strip() or i["attachment"] for i in items):
            return JSONResponse({"error": "No items found in file"}, status_code=400)
        if len(items) > MAX_IMPORT_ITEMS:
            return JSONResponse(
                {"error": f"Import exceeds maximum of {MAX_IMPORT_ITEMS} items"},
                status_code=400,
            )

        # Bundle referenced attachments (archives only) as data URLs so the draft
        # table can rebuild them client-side exactly like a manual file upload.
        attachments = []
        if zf is not None:
            referenced = {i["attachment"] for i in items if i["attachment"]}
            for fname in sorted(referenced & _archive_attachment_names(zf)):
                data = zf.read(f"attachments/{fname}")
                ext = os.path.splitext(fname)[1].lstrip(".")
                mime = _guess_mime(ext)
                attachments.append(
                    {
                        "filename": fname,
                        "name": fname,
                        "extension": ext,
                        "mime": mime,
                        "modality": _mime_to_modality(mime),
                        "file_size": len(data),
                        "data_url": (
                            f"data:{mime};base64,"
                            + base64.b64encode(data).decode("ascii")
                        ),
                    }
                )

        return JSONResponse(
            {
                "name": (manifest.get("name") or "").strip(),
                "description": manifest.get("description") or "",
                "type": TEST_TYPE_MAP.get(
                    (manifest.get("type") or "EVALUATION").upper(), 1
                ),
                "provider": manifest.get("provider") or "",
                "labels": _coerce_str_list(manifest.get("labels")),
                "items": items,
                "attachments": attachments,
            }
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return JSONResponse({"error": f"Parse failed: {e}"}, status_code=500)


_MIME_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    "xml": "text/xml",
    "md": "text/markdown",
}


def _guess_mime(ext: str) -> str:
    return _MIME_MAP.get(ext.lower(), "application/octet-stream")


def _mime_to_modality(mime: str) -> int:
    if mime.startswith("audio/"):
        return 1
    if mime.startswith("image/"):
        return 2
    if mime.startswith("video/"):
        return 3
    if mime.startswith("text/"):
        return 4
    return 0


@router.get("/tests/{test_id}/edit")
async def edit_test(
    test_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )

        if not (user.is_admin or test_pb.owner == user.email):
            return templates.TemplateResponse(
                request, "404.html", {"is_admin": user.is_admin}, status_code=403
            )

        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )

        attachment_ids = list(
            {item.attachment_id for item in test_item_pbs if item.attachment_id}
        )
        attachment_metas = {}
        if attachment_ids:
            att_pbs = await loop.run_in_executor(
                None,
                lambda: list(
                    stub.BatchGetAttachments(
                        service_pb2.BatchGetAttachmentsRequest(
                            ids=attachment_ids, metadata_only=True
                        ),
                        metadata=metadata,
                    )
                ),
            )
            attachment_metas = {
                a.id: {
                    "id": a.id,
                    "name": a.name,
                    "extension": a.extension,
                    "modality": a.modality,
                    "file_size": a.file_size,
                    "mime": a.mime,
                }
                for a in att_pbs
            }

        return templates.TemplateResponse(
            request,
            "create_test.html",
            {
                "page_title": "Pelidum MPAC - Edit Test",
                "default_provider": test_pb.provider,
                "is_admin": user.is_admin,
                "edit_mode": True,
                "test_pb": test_pb,
                "test_item_pbs": test_item_pbs,
                "attachment_metas": attachment_metas,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.post("/tests/{test_id}/edit/submit")
async def submit_edit_test(
    test_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        data = await request.json()
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )

        if not (user.is_admin or test_pb.owner == user.email):
            return JSONResponse(
                {"success": False, "error": "Permission denied"}, status_code=403
            )

        items = data.get("items", [])
        valid_items = [i for i in items if i.get("question", "").strip()]

        test_pb.name = data.get("name", test_pb.name)
        test_pb.description = data.get("description", test_pb.description)
        test_pb.type = data.get("type", test_pb.type)
        test_pb.provider = data.get("provider", test_pb.provider)
        test_pb.item_count = len(valid_items)
        del test_pb.labels[:]
        test_pb.labels.extend(data.get("labels", []))

        await loop.run_in_executor(
            None, lambda: stub.UpdateTest(test_pb, metadata=metadata)
        )

        attachments_map = {}
        for attachment_data in data.get("attachments", []):
            file_bytes = base64.b64decode(attachment_data.get("file"))
            attachment_pb = service_pb2.FileAttachment(
                name=attachment_data.get("name"),
                extension=attachment_data.get("extension"),
                modality=attachment_data.get("modality"),
                file=file_bytes,
                file_size=attachment_data.get("file_size"),
                mime=attachment_data.get("mime"),
            )
            attachment_response = await loop.run_in_executor(
                None,
                lambda pb=attachment_pb: stub.CreateAttachment(pb, metadata=metadata),
            )
            attachments_map[attachment_data.get("id")] = attachment_response.id

        existing_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(parent_id=test_id, limit=10000),
                    metadata=metadata,
                )
            ),
        )
        existing_ids = {item.id for item in existing_item_pbs}
        submitted_server_ids = {
            i["server_id"] for i in valid_items if i.get("server_id")
        }

        for old_id in existing_ids - submitted_server_ids:
            await loop.run_in_executor(
                None,
                lambda oid=old_id: stub.DeleteTestItem(
                    service_pb2.DeleteRequest(id=oid), metadata=metadata
                ),
            )

        for item_data in valid_items:
            attachment_preid = item_data.get("attachment_id")
            attachment_id = (
                attachments_map.get(attachment_preid, attachment_preid)
                if attachment_preid
                else ""
            )

            server_id = item_data.get("server_id")
            if server_id and server_id in existing_ids:
                item_pb = service_pb2.TestItem(
                    id=server_id,
                    test_id=test_id,
                    question=item_data.get("question"),
                    context=item_data.get("context"),
                    choices=item_data.get("choices"),
                    answer=item_data.get("answer"),
                    is_relevant=item_data.get("is_relevant"),
                    attachment_id=attachment_id,
                )
                await loop.run_in_executor(
                    None,
                    lambda pb=item_pb: stub.UpdateTestItem(pb, metadata=metadata),
                )
            else:
                item_pb = service_pb2.TestItem(
                    test_id=test_id,
                    question=item_data.get("question"),
                    context=item_data.get("context"),
                    choices=item_data.get("choices"),
                    answer=item_data.get("answer"),
                    is_relevant=item_data.get("is_relevant"),
                    attachment_id=attachment_id,
                )
                await loop.run_in_executor(
                    None,
                    lambda pb=item_pb: stub.CreateTestItem(pb, metadata=metadata),
                )

        return JSONResponse(
            {
                "status": "ok",
                "test_id": test_id,
                "message": f"Test '{data.get('name')}' updated with {len(valid_items)} items",
            }
        )
    except Exception as e:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Unable to update test"}, status_code=500
        )


@router.get("/tests/{test_id}")
async def test_details(
    test_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )
        default_limit = 50
        try:
            limit_param = int(request.query_params.get("num_items", default_limit))
        except ValueError:
            limit_param = default_limit

        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(parent_id=test_pb.id, limit=limit_param),
                    metadata=metadata,
                )
            ),
        )

        attachment_ids = list(
            {item.attachment_id for item in test_item_pbs if item.attachment_id}
        )
        attachments_map = {}
        if attachment_ids:
            try:
                attachment_pbs = await loop.run_in_executor(
                    None,
                    lambda: list(
                        stub.BatchGetAttachments(
                            service_pb2.BatchGetAttachmentsRequest(
                                ids=attachment_ids, metadata_only=True
                            ),
                            metadata=metadata,
                        )
                    ),
                )
                attachments_map = {a.id: a for a in attachment_pbs}
            except Exception as e:
                logging.warning(f"Failed to fetch attachment metadata: {e}")

        return templates.TemplateResponse(
            request,
            "test_details.html",
            {
                "test_pb": test_pb,
                "test_item_pbs": test_item_pbs,
                "attachments_map": attachments_map,
                "is_admin": user.is_admin,
                "user_email": user.email,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.post("/tests/{test_id}/delete")
async def test_delete(
    test_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        await loop.run_in_executor(
            None,
            lambda: stub.DeleteTest(
                service_pb2.DeleteRequest(id=test_id), metadata=metadata
            ),
        )
        return JSONResponse({"success": True})
    except grpc.RpcError as e:
        logging.error(f"gRPC error deleting test: {e}")
        return JSONResponse(
            {"success": False, "error": f"Server error: {e.details()}"},
            status_code=500,
        )
    except Exception:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Error processing your request"},
            status_code=500,
        )


VISIBILITY_MAP = {
    "PUBLIC": 4,
    "ORG_ONLY": 2,
    "OWNER_ONLY": 1,
    "SPECIFIED_USERS": 3,
    "ADMIN": 5,
}


@router.post("/tests/{test_id}/visibility")
async def test_update_visibility(
    test_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        data = await request.json()
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=metadata),
        )

        vis_type = VISIBILITY_MAP.get(data.get("visibility_type", ""), 0)
        allowed_users = data.get("allowed_users", [])

        test_pb.visibility.type = vis_type
        del test_pb.visibility.allowed_users[:]
        test_pb.visibility.allowed_users.extend(allowed_users)

        await loop.run_in_executor(
            None,
            lambda: stub.UpdateTest(test_pb, metadata=metadata),
        )
        return JSONResponse({"success": True})
    except grpc.RpcError as e:
        logging.error(f"gRPC error updating test visibility: {e}")
        return JSONResponse(
            {"success": False, "error": f"Server error: {e.details()}"},
            status_code=500,
        )
    except Exception:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Error processing your request"},
            status_code=500,
        )
