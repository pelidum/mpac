import grpc
import traceback
import uuid

from absl import logging
from werkzeug.security import check_password_hash

from server import service_pb2

# Werkzeug-format scrypt hash that never matches any password.
_DUMMY_PASSWORD_HASH = "scrypt:32768:8:1$bRT3PWGCcnSO0ETo$" + "0" * 128

DEFAULT_DAILY_SPEND_CENTS = 1000  # $10/day
DEFAULT_MONTHLY_SPEND_CENTS = 10000  # $100/month


class UsersMixin:
    async def GetUser(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.User:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = (
            await self.is_user_admin(user_id=request_owner) if request_owner else False
        )
        if not request_owner_admin and request.id != request_owner:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Access denied",
            )

        try:
            user_pb = service_pb2.User()
            query_string = """
                SELECT
                    proto_bytes
                FROM users
                WHERE
                    proto_jsonb ->> 'id' = $1
            """
            query_values = (request.id,)
            try:
                async with self.db_pool.acquire() as conn:
                    proto_result = await conn.fetchrow(query_string, *query_values)
                    user_bytes = None
                    if proto_result:
                        user_bytes = proto_result[0]

                    if user_bytes:
                        user_pb.ParseFromString(user_bytes)

            except Exception as e:
                logging.error(traceback.format_exc())
                logging.error(f"Unable to fetch object: {e}")
                return None

            daily_usd, monthly_usd = await self._get_current_spend(user_pb.id)
            user_pb.spend.daily_usd = daily_usd
            user_pb.spend.monthly_usd = monthly_usd

            user_pb.ClearField("password_hash")
            if user_pb.HasField("api_key"):
                user_pb.api_key.ClearField("hash")

            return user_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def CreateUser(
        self,
        request: service_pb2.User,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        if request_owner_admin:
            request.created_at_utc.GetCurrentTime()
            request.modified_at_utc.GetCurrentTime()
            request.last_login.FromSeconds(0)  # Denote that they have never logged in
            request.api_key.CopyFrom(service_pb2.UserAPIKey())
            if request.role != service_pb2.UserRole.ADMIN:
                if not request.rate_limits.daily_spend:
                    request.rate_limits.daily_spend = DEFAULT_DAILY_SPEND_CENTS
                if not request.rate_limits.monthly_spend:
                    request.rate_limits.monthly_spend = DEFAULT_MONTHLY_SPEND_CENTS
            response_pb = await self._grpc_create(
                id=request.id,
                proto_obj=request,
                obj_type="users",
            )

            return response_pb

        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only admins can create new users.",
            )

    async def _get_stored_user(self, user_id: str) -> service_pb2.User | None:
        """Read the raw stored User proto, including sensitive fields.

        Bypasses GetUser (which strips password_hash / api_key.hash) so that
        server-side code can preserve credentials across updates.  The result
        must never be returned to a client.
        """
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT proto_bytes FROM users WHERE id = $1 LIMIT 1;",
                    user_id,
                )
            if row:
                pb = service_pb2.User()
                pb.ParseFromString(row[0])
                return pb
        except Exception as e:
            logging.error(f"_get_stored_user failed for {user_id}: {e}")
        return None

    async def UpdateUser(
        self,
        request: service_pb2.User,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)
        original_pb = await self.GetUser(
            context=context,
            request=service_pb2.GetRequest(id=request.id),
        )
        if (request_owner == original_pb.id) or request_owner_admin:
            request.modified_at_utc.GetCurrentTime()
            request.created_at_utc.CopyFrom(original_pb.created_at_utc)

            stored_pb = await self._get_stored_user(request.id)
            if stored_pb:
                if not request.password_hash and stored_pb.password_hash:
                    request.password_hash = stored_pb.password_hash
                if not request.api_key.hash and stored_pb.api_key.hash:
                    request.api_key.hash = stored_pb.api_key.hash
                if not request_owner_admin:
                    request.role = stored_pb.role
                    request.rate_limits.CopyFrom(stored_pb.rate_limits)
                    request.org_domain = stored_pb.org_domain
                    request.ClearField("spend")

            response_pb = await self._grpc_update(
                id=request.id, proto_obj=request, obj_type="users"
            )
            self._invalidate_rate_limits_cache(request.id)
            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "No credentials provided."
            )

    async def DeleteUser(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)
        if request_owner_admin:
            response_pb = await self._grpc_delete(
                id=request.id,
                obj_type="users",
                obj_owner=request_owner,
            )
            return response_pb

    async def ListUsers(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        request_owner = await self.get_request_owner(request)
        is_admin = (
            await self.is_user_admin(user_id=request_owner) if request_owner else False
        )

        try:
            user_bytes_results = await self._grpc_list(
                obj_type="users",
                limit=request.limit,
            )
            for user_bytes in user_bytes_results:
                user_pb = service_pb2.User()
                user_pb.ParseFromString(user_bytes)
                user_pb.ClearField("password_hash")
                if is_admin:
                    if user_pb.HasField("api_key"):
                        user_pb.api_key.ClearField("hash")
                else:
                    user_pb.ClearField("api_key")
                    user_pb.ClearField("rate_limits")
                    user_pb.ClearField("spend")
                    user_pb.ClearField("last_login")
                yield user_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def LoginUser(
        self,
        request: service_pb2.LoginRequest,
        context: grpc.aio.ServicerContext,
    ):
        if not request.user_id or not request.password:
            logging.warning("Login attempt with missing credentials")
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Username and password are required",
            )

        if len(request.user_id) > 255 or len(request.password) > 255:
            logging.warning(
                f"Login attempt with oversized credentials for user: {request.user_id[:50]}"
            )
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Invalid credentials",
            )

        user_pb = None
        query_string = """
            SELECT
                proto_bytes
            FROM users
            WHERE
                proto_jsonb ->> 'id' = $1
        """
        query_values = (request.user_id,)

        try:
            async with self.db_pool.acquire() as conn:
                proto_result = await conn.fetchrow(query_string, *query_values)

            if proto_result and proto_result[0]:
                user_pb = service_pb2.User()
                user_pb.ParseFromString(proto_result[0])

        except Exception as e:
            logging.error(
                f"Database error during login: {e} | {traceback.format_exc()}"
            )
            await context.abort(
                grpc.StatusCode.INTERNAL,
                "Internal server error",
            )

        if user_pb is None:
            logging.warning(f"Login attempt for non-existent user: {request.user_id}")
            # Verify a dummy hash to prevent timing attacks
            check_password_hash(_DUMMY_PASSWORD_HASH, request.password)
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid username or password",
            )

        if not user_pb or not user_pb.id:
            logging.error(f"User object empty after fetch for user: {request.user_id}")
            await context.abort(
                grpc.StatusCode.INTERNAL,
                "Internal server error",
            )

        if not user_pb.password_hash:
            logging.error(f"User {request.user_id} has no password hash set")
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid username or password",
            )

        if not check_password_hash(user_pb.password_hash, request.password):
            logging.warning(f"Failed login attempt for user: {request.user_id}")
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid username or password",
            )
            return

        logging.info(f"Successful login for user: {request.user_id}")
        user_pb.ClearField("password_hash")
        if user_pb.HasField("api_key"):
            user_pb.api_key.ClearField("hash")
        return user_pb
