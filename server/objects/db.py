import asyncpg
import difflib
import filetype
import hashlib
import json
import math
import os
import re
import traceback
import uuid

from absl import logging
from google.protobuf.json_format import MessageToJson
from werkzeug.security import generate_password_hash

from server import service_pb2
from server.objects.auth import USER_EMAIL_CONTEXT

MPAC_OBJECTS = {
    "attachments": service_pb2.FileAttachment(),
    "backends": service_pb2.Backend(),
    "benchmarks": service_pb2.Benchmark(),
    "test_items": service_pb2.TestItem(),
    "test_runs": service_pb2.TestRun(),
    "test_run_answers": service_pb2.TestRunAnswer(),
    "tests": service_pb2.Test(),
    "users": service_pb2.User(),
}


class DBMixin:
    async def connect_db(
        self,
        admin_onboarding_id: str | None = None,
        admin_onboarding_password: str | None = None,
        admin_onboarding_api_key: str | None = None,
    ):
        try:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    for object_name in MPAC_OBJECTS.keys():
                        table_creation_sql = f"""
                        CREATE TABLE IF NOT EXISTS {object_name} (
                            id VARCHAR PRIMARY KEY,
                            proto_jsonb JSONB,
                            proto_bytes BYTEA,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                        """
                        try:
                            await conn.execute(table_creation_sql)
                        except asyncpg.exceptions.UniqueViolationError:
                            pass

                    # Create indexes for any JSONB fields we hit regularly
                    index_creation_sql = """
                        CREATE INDEX IF NOT EXISTS idx_testitems_parent_id
                        ON test_items ((proto_jsonb ->> 'parent_id'));

                        CREATE INDEX IF NOT EXISTS idx_testitems_test_id
                        ON test_items ((proto_jsonb ->> 'test_id'));

                        CREATE INDEX IF NOT EXISTS idx_users_api_key_hash
                        ON users ((proto_jsonb -> 'api_key' ->> 'hash'));

                        CREATE INDEX IF NOT EXISTS idx_backends_type
                        ON backends ((proto_jsonb ->> 'backend_type'));

                        CREATE INDEX IF NOT EXISTS idx_backends_enabled
                        ON backends ((proto_jsonb ->> 'enabled'));

                        CREATE INDEX IF NOT EXISTS idx_test_runs_owner
                        ON test_runs ((proto_jsonb ->> 'owner'));

                        CREATE INDEX IF NOT EXISTS idx_benchmarks_status
                        ON benchmarks ((proto_jsonb ->> 'status'));

                        CREATE INDEX IF NOT EXISTS idx_tests_owner
                        ON tests ((proto_jsonb ->> 'owner'));

                        CREATE INDEX IF NOT EXISTS idx_tests_visibility_type
                        ON tests ((proto_jsonb -> 'visibility' ->> 'type'));

                        CREATE INDEX IF NOT EXISTS idx_attachments_owner
                        ON attachments ((proto_jsonb ->> 'owner'));

                        CREATE INDEX IF NOT EXISTS idx_attachments_visibility_type
                        ON attachments ((proto_jsonb -> 'visibility' ->> 'type'));

                        CREATE INDEX IF NOT EXISTS idx_benchmarks_owner
                        ON benchmarks ((proto_jsonb ->> 'owner'));
                    """
                    try:
                        await conn.execute(index_creation_sql)
                    except Exception as e:
                        logging.error(e)

                    await conn.execute("""
                        ALTER TABLE tests
                        ADD COLUMN IF NOT EXISTS item_count INTEGER NOT NULL DEFAULT 0;
                    """)

                    await conn.execute("""
                        UPDATE tests t
                        SET item_count = sub.cnt
                        FROM (
                            SELECT proto_jsonb ->> 'test_id' AS test_id, COUNT(*) AS cnt
                            FROM test_items
                            GROUP BY 1
                        ) sub
                        WHERE t.id = sub.test_id
                          AND t.item_count = 0;
                    """)

                    await conn.execute("""
                        ALTER TABLE test_runs
                        ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
                    """)

                    # TPM tracking table — not a proto object, managed separately
                    tpm_table_sql = """
                        CREATE TABLE IF NOT EXISTS user_tpm_windows (
                            user_id TEXT NOT NULL,
                            window_minute BIGINT NOT NULL,
                            tokens_used BIGINT NOT NULL DEFAULT 0,
                            PRIMARY KEY (user_id, window_minute)
                        );
                    """
                    try:
                        await conn.execute(tpm_table_sql)
                    except Exception as e:
                        logging.error(e)

            if admin_onboarding_id:
                try:
                    async with self.db_pool.acquire() as db_conn:
                        admin_exists = await db_conn.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM users WHERE proto_jsonb->>'role' = 'ADMIN' LIMIT 1);"
                        )
                    if admin_exists:
                        logging.info(
                            "Admin user(s) already exist; skipping onboarding."
                        )
                    else:
                        logging.info(f"Onboarding admin {admin_onboarding_id}...")
                        admin_onboarding_pb = service_pb2.User()
                        admin_onboarding_pb.id = admin_onboarding_id
                        admin_onboarding_pb.role = service_pb2.UserRole.ADMIN
                        admin_onboarding_pb.created_at_utc.GetCurrentTime()
                        admin_onboarding_pb.modified_at_utc.GetCurrentTime()
                        admin_onboarding_pb.last_login.GetCurrentTime()
                        admin_onboarding_pb.rate_limits.CopyFrom(
                            service_pb2.UserRateLimits()
                        )
                        if admin_onboarding_api_key:
                            admin_onboarding_pb.api_key.hash = hashlib.sha256(
                                admin_onboarding_api_key.encode()
                            ).hexdigest()
                            admin_onboarding_pb.api_key.active = True
                            admin_onboarding_pb.api_key.created_at_utc.GetCurrentTime()
                        else:
                            admin_onboarding_pb.api_key.CopyFrom(
                                service_pb2.UserAPIKey()
                            )
                        if admin_onboarding_password:
                            admin_onboarding_pb.password_hash = generate_password_hash(
                                admin_onboarding_password
                            )

                        create_result = await self._grpc_create(
                            id=admin_onboarding_id,
                            proto_obj=admin_onboarding_pb,
                            obj_type="users",
                        )
                        if create_result.code == service_pb2.ResponseCode.SUCCESS:
                            logging.info(f"Created admin {admin_onboarding_id}.")
                        else:
                            async with self.db_pool.acquire() as db_conn:
                                row = await db_conn.fetchrow(
                                    "SELECT proto_bytes FROM users WHERE id = $1 LIMIT 1;",
                                    admin_onboarding_id,
                                )
                            if row:
                                existing_pb = service_pb2.User()
                                existing_pb.ParseFromString(row[0])
                                existing_pb.role = service_pb2.UserRole.ADMIN
                                if admin_onboarding_password:
                                    existing_pb.password_hash = generate_password_hash(
                                        admin_onboarding_password
                                    )
                                if admin_onboarding_api_key:
                                    existing_pb.api_key.hash = hashlib.sha256(
                                        admin_onboarding_api_key.encode()
                                    ).hexdigest()
                                    existing_pb.api_key.active = True
                                    existing_pb.api_key.created_at_utc.GetCurrentTime()
                                existing_pb.modified_at_utc.GetCurrentTime()
                                await self._grpc_update(
                                    id=admin_onboarding_id,
                                    proto_obj=existing_pb,
                                    obj_type="users",
                                )
                                logging.info(
                                    f"Updated existing admin {admin_onboarding_id}."
                                )
                except Exception as e:
                    logging.error(traceback.format_exc())
                    logging.error(f"Admin onboarding failed: {e}")

        except Exception as e:
            logging.error(f"Failed to create asyncpg pool: {e}")
            raise

    def attachment_parser(
        self,
        file_name: str,
        file_path: str,
        owner: str,
    ) -> service_pb2.FileAttachment:
        attachment_pb = service_pb2.FileAttachment()
        attachment_filetype_metadata = filetype.guess(file_path)

        if attachment_filetype_metadata is None:
            try:
                with open(file_path, "rb") as f:
                    f.read().decode("utf-8")
                attachment_pb.modality = service_pb2.FileModality.TEXT
                attachment_mime = "text/plain"
                attachment_extension = (
                    os.path.splitext(file_name)[1].lstrip(".") or "txt"
                )
            except (UnicodeDecodeError, IOError):
                return attachment_pb  # unrecognized binary, return empty
        else:
            attachment_type = attachment_filetype_metadata.mime.split("/")[0]
            match attachment_type:
                case "audio":
                    attachment_pb.modality = service_pb2.FileModality.AUDIO
                case "image":
                    attachment_pb.modality = service_pb2.FileModality.IMAGE
                case "video":
                    attachment_pb.modality = service_pb2.FileModality.VIDEO
                case _:
                    attachment_pb.modality = (
                        service_pb2.FileModality.UNSPECIFIED_MODALITY
                    )
            attachment_mime = attachment_filetype_metadata.mime
            attachment_extension = attachment_filetype_metadata.extension

        if attachment_pb.modality != service_pb2.FileModality.UNSPECIFIED_MODALITY:
            with open(file_path, "rb") as attachment_file_handler:
                file_content = attachment_file_handler.read()
                attachment_pb.id = hashlib.sha256(file_content).hexdigest()
                attachment_pb.name = file_name
                attachment_pb.extension = attachment_extension
                attachment_pb.file = file_content
                attachment_pb.file_size = math.ceil(len(file_content) / 1024)
                attachment_acls = service_pb2.Visibility()
                attachment_acls.type = (
                    service_pb2.Visibility.VisibilityOptions.VISIBILITY_ORG_ONLY
                )
                attachment_pb.visibility.CopyFrom(attachment_acls)
                attachment_pb.owner = owner
                attachment_pb.mime = attachment_mime

        return attachment_pb

    def calculate_confidence(self, consensus_score: float) -> str:
        if consensus_score == 1:
            return "⭐⭐⭐⭐⭐ Unanimous consensus"
        elif consensus_score >= 0.75:
            return "⭐⭐⭐⭐ Strong consensus"
        elif consensus_score >= 0.6:
            return "⭐⭐⭐ Majority consensus"
        elif consensus_score >= 0.4:
            return "⭐⭐ Weak consensus"
        else:
            return "⭐ No consensus"

    def calculate_grade(self, score: float) -> str:
        if score >= 0.95:
            return "⭐⭐⭐⭐⭐ High Quality"
        elif score >= 0.8:
            return "⭐⭐⭐⭐ OK Quality"
        elif score >= 0.7:
            return "⭐⭐⭐ Low Quality"
        elif score >= 0.5:
            return "⭐⭐ Very Low Quality"
        else:
            return "⭐ Garbage"

    def estimate_local_inference_cost(
        self,
        duration_seconds: int,
    ) -> dict:
        avg_power_watts = 300
        kwh_price = 0.14
        hardware_cost = 2000
        hardware_lifespan_years = 4.0
        annual_usage_hours = 2000

        kwh_consumed = (avg_power_watts * duration_seconds) / (1000.0 * 3600.0)
        energy_cost = kwh_consumed * kwh_price

        total_useful_seconds = hardware_lifespan_years * annual_usage_hours * 3600.0
        depreciation_cost = (hardware_cost / total_useful_seconds) * duration_seconds

        total_cost = energy_cost + depreciation_cost
        return total_cost

    async def get_request_owner(self, request) -> str:
        return USER_EMAIL_CONTEXT.get()

    async def is_user_admin(self, user_id) -> bool:
        query_string = """
            SELECT (proto_jsonb ->> 'id')::text AS id
            FROM users
            WHERE proto_jsonb @> '{"role": "ADMIN"}';
        """
        try:
            async with self.db_pool.acquire() as conn:
                admin_ids_result = await conn.fetch(query_string)
                admin_ids = [x["id"] for x in admin_ids_result]
                if user_id in admin_ids:
                    return True
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Unable to fetch admin list: {e}")
            return False

    async def _validate_answer(self, raw_answer, choices):
        stripped = re.sub(
            r"<think>.*?</think>",
            "",
            raw_answer,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        stripped = re.sub(r'^["\']|["\']$', "", stripped).strip()
        stripped = re.sub(r"^```\w*\n", "", stripped)
        stripped = re.sub(r"\n```$", "", stripped)
        stripped = re.sub(r"^```(.+?)```$", r"\1", stripped)
        stripped = stripped.strip()
        sanitized_choices = [c.strip().lower() for c in choices]
        longest_first = sorted(
            range(len(sanitized_choices)),
            key=lambda i: len(sanitized_choices[i]),
            reverse=True,
        )

        def _match(candidate: str) -> str:
            low = candidate.lower().strip()
            for i in longest_first:
                if low == sanitized_choices[i]:
                    return choices[i]
            for i in longest_first:
                if low.startswith(sanitized_choices[i]) or low.endswith(
                    sanitized_choices[i]
                ):
                    return choices[i]
            contained = [i for i, sc in enumerate(sanitized_choices) if sc in low]
            if len(contained) == 1:
                return choices[contained[0]]
            close = difflib.get_close_matches(low, sanitized_choices, n=1, cutoff=0.6)
            if close:
                return choices[sanitized_choices.index(close[0])]
            return ""

        def _extract_json_candidate(text: str) -> str:
            """Small models (2B/4B) often output structured JSON instead of plain text.
            Try to extract a label-like value from the JSON object."""
            text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL
            )
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for key in (
                        "label",
                        "answer",
                        "choice",
                        "prediction",
                        "response",
                        "output",
                        "result",
                        "category",
                        "class",
                    ):
                        val = obj.get(key)
                        if val is not None:
                            return str(val).strip()
                    # Last resort: first string value
                    for val in obj.values():
                        if isinstance(val, str):
                            return val.strip()
            except (json.JSONDecodeError, ValueError):
                pass
            return ""

        first_line = next(
            (ln.strip() for ln in stripped.splitlines() if ln.strip()), stripped
        )

        for candidate in (
            first_line,
            _extract_json_candidate(first_line),
            _extract_json_candidate(stripped),
            stripped,
        ):
            if not candidate:
                continue
            answer = _match(candidate)
            if answer:
                return answer

        logging.warning(
            f"_validate_answer: no match found. "
            f"first_line={first_line!r:.120} choices={choices}"
        )
        return ""

    def _visibility_where_clause(
        self,
        requesting_user: str,
        is_admin: bool,
        param_offset: int = 0,
        table_alias: str = None,
    ) -> tuple[str, list]:
        if is_admin:
            return ("TRUE", [])

        col = f"{table_alias}.proto_jsonb" if table_alias else "proto_jsonb"
        p = param_offset + 1
        return (
            f"""(
                {col}->'visibility' IS NULL
                OR {col}->'visibility'->>'type' IS NULL
                OR {col}->'visibility'->>'type' = 'VISIBILITY_UNSET'
                OR {col}->'visibility'->>'type' = 'VISIBILITY_PUBLIC'
                OR {col}->'visibility'->>'type' = 'VISIBILITY_ORG_ONLY'
                OR (
                    {col}->'visibility'->>'type' = 'VISIBILITY_SPECIFIED_USERS'
                    AND (
                        {col}->>'owner' = ${p}
                        OR {col}->'visibility'->'allowed_users' ? ${p}
                    )
                )
                OR (
                    {col}->'visibility'->>'type' = 'VISIBILITY_OWNER_ONLY'
                    AND {col}->>'owner' = ${p}
                )
            )""",
            [requesting_user],
        )

    async def _grpc_get(
        self,
        id: str,
        obj_type: str,
        obj_owner: str = None,
    ):
        if obj_type not in MPAC_OBJECTS.keys():
            logging.error(f"Invalid MPAC object type: {obj_type}")
            return None

        if self.db_pool is None:
            logging.error("Database pool is not initialized.")
            return None

        if obj_owner is None:
            obj_owner = USER_EMAIL_CONTEXT.get()

        is_admin = await self.is_user_admin(user_id=obj_owner) if obj_owner else False

        try:
            async with self.db_pool.acquire() as conn:
                if is_admin:
                    proto_result = await conn.fetchrow(
                        f"SELECT proto_bytes FROM {obj_type} WHERE id = $1 LIMIT 1;",
                        id,
                    )
                else:
                    vis_clause, vis_params = self._visibility_where_clause(
                        requesting_user=obj_owner,
                        is_admin=False,
                        param_offset=1,
                    )
                    proto_result = await conn.fetchrow(
                        f"SELECT proto_bytes FROM {obj_type} WHERE id = $1 AND {vis_clause} LIMIT 1;",
                        id,
                        *vis_params,
                    )
                return proto_result[0] if proto_result else None

        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Unable to fetch object: {e}")
            return None

    async def _grpc_create(
        self,
        id: str,
        proto_obj,
        obj_type: str,
        overwrite: bool = False,
    ) -> service_pb2.StatusReply:
        response_pb = service_pb2.StatusReply()
        response_pb.id = id

        if obj_type not in MPAC_OBJECTS.keys():
            logging.error(f"Invalid MPAC object type: {obj_type}")
            response_pb.code = service_pb2.ResponseCode.ERROR
            return response_pb

        try:
            proto_json = MessageToJson(
                message=proto_obj,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
                indent=0,
            )
            proto_bytes = proto_obj.SerializeToString()

            async with self.db_pool.acquire() as conn:
                query_string = f"""
                    INSERT INTO {obj_type} (id, proto_jsonb, proto_bytes)
                    VALUES ($1, $2, $3)
                    """
                await conn.execute(query_string, id, proto_json, proto_bytes)
                response_pb.code = service_pb2.ResponseCode.SUCCESS

        except asyncpg.exceptions.UniqueViolationError:
            if overwrite:
                return await self._grpc_update(
                    id=id,
                    proto_obj=proto_obj,
                    obj_type=obj_type,
                )
            else:
                response_pb.code = service_pb2.ResponseCode.ERROR
                response_pb.reason = "Object already exists"

        except Exception as e:
            logging.error(f"Unable to create object: {e}")
            response_pb.code = service_pb2.ResponseCode.ERROR

        return response_pb

    async def _grpc_update(
        self,
        id: str,
        proto_obj,
        obj_type: str,
        obj_owner: str = None,
    ) -> service_pb2.StatusReply:
        response_pb = service_pb2.StatusReply()
        response_pb.id = id

        if obj_type not in MPAC_OBJECTS.keys():
            logging.error(f"Invalid MPAC object type: {obj_type}")
            response_pb.code = service_pb2.ResponseCode.ERROR
            return response_pb

        proto_json = MessageToJson(
            message=proto_obj,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
            indent=0,
        )
        proto_bytes = proto_obj.SerializeToString()

        try:
            async with self.db_pool.acquire() as conn:
                query_string = f"""
                    UPDATE {obj_type}
                    SET
                        proto_jsonb = $1,
                        proto_bytes = $2
                    WHERE
                        id = $3;
                    """

                await conn.execute(query_string, proto_json, proto_bytes, id)
                response_pb.code = service_pb2.ResponseCode.SUCCESS

        except Exception as e:
            logging.error(f"Unable to update object: {e}")
            response_pb.code = service_pb2.ResponseCode.ERROR

        return response_pb

    async def _grpc_delete(
        self,
        id: str,
        obj_type: str,
        obj_owner: str,
    ) -> service_pb2.StatusReply:
        response_pb = service_pb2.StatusReply()
        response_pb.id = id
        if obj_type not in MPAC_OBJECTS.keys():
            logging.error(f"Invalid MPAC object type: {obj_type}")
            response_pb.code = service_pb2.ResponseCode.ERROR
            return response_pb

        try:
            async with self.db_pool.acquire() as conn:
                request_owner_admin = await self.is_user_admin(user_id=obj_owner)
                obj_query_string = f"SELECT proto_jsonb FROM {obj_type} WHERE id = $1"
                obj_query_values = (id,)
                obj_json_response = await conn.fetchrow(
                    obj_query_string, *obj_query_values
                )
                obj_json = json.loads(obj_json_response["proto_jsonb"])
                obj_json_id = obj_json.get("id")
                obj_json_owner = obj_json.get("owner")

                if (
                    request_owner_admin
                    or (obj_json_owner == obj_owner)
                    or (obj_json_id == obj_owner)
                ):
                    query_string = f"""
                        DELETE FROM {obj_type}
                        WHERE id = $1;
                        """

                    await conn.execute(query_string, id)
                    response_pb.code = service_pb2.ResponseCode.SUCCESS
                else:
                    response_pb.code = service_pb2.ResponseCode.ERROR

        except Exception as e:
            logging.error(f"Unable to delete object: {e}")
            response_pb.code = service_pb2.ResponseCode.ERROR

        return response_pb

    async def _grpc_list(
        self,
        obj_type: str,
        obj_owner: str = None,
        limit: int = 50,
    ) -> list[bytes]:
        proto_bytes_results = []
        if obj_type not in MPAC_OBJECTS.keys():
            logging.error(f"Invalid MPAC object type: {obj_type}")
            return proto_bytes_results

        if limit == 0:
            limit = 50

        if obj_owner is None:
            obj_owner = USER_EMAIL_CONTEXT.get()

        is_admin = await self.is_user_admin(user_id=obj_owner) if obj_owner else False

        try:
            async with self.db_pool.acquire() as conn:
                if is_admin:
                    query_string = f"""
                        SELECT proto_bytes
                        FROM {obj_type}
                        ORDER BY created_at DESC
                        LIMIT $1;
                        """
                    proto_results = await conn.fetch(query_string, limit)
                else:
                    vis_clause, vis_params = self._visibility_where_clause(
                        requesting_user=obj_owner,
                        is_admin=False,
                        param_offset=0,
                    )
                    limit_param = f"${len(vis_params) + 1}"
                    query_string = f"""
                        SELECT proto_bytes
                        FROM {obj_type}
                        WHERE {vis_clause}
                        ORDER BY created_at DESC
                        LIMIT {limit_param};
                        """
                    proto_results = await conn.fetch(query_string, *vis_params, limit)

                if proto_results:
                    proto_bytes_results.extend([x[0] for x in proto_results])

        except Exception as e:
            logging.error(f"Unable to list objects: {e}")

        return proto_bytes_results
