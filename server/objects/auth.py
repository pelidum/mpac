import asyncpg
import contextvars
import grpc
import hashlib
import os

import jwt as pyjwt

from typing import Awaitable, Callable

from absl import logging
from cachetools import TTLCache

USER_EMAIL_CONTEXT = contextvars.ContextVar("user_email", default=None)
WEB_UI_CONTEXT = contextvars.ContextVar("is_web_ui_call", default=False)

MPAC_JWT_SECRET = os.getenv("MPAC_JWT_SECRET")


class ApiAuthClient(grpc.aio.ServerInterceptor):
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.auth_cache = TTLCache(maxsize=1000, ttl=60)

    async def key_lookup(self, api_key: str) -> str | None:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        if key_hash in self.auth_cache:
            return self.auth_cache[key_hash]

        query_string = """
            SELECT id FROM users
            WHERE (proto_jsonb->'api_key'->>'hash') = $1::text
            LIMIT 1;
        """

        async with self.db_pool.acquire() as conn:
            user = await conn.fetchrow(query_string, key_hash)
            user_id = user["id"] if user else None
            if user_id:
                self.auth_cache[key_hash] = user_id
            return user_id

    async def _abort(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]
        ],
        handler_call_details: grpc.HandlerCallDetails,
        message: str,
        code: grpc.StatusCode = grpc.StatusCode.UNAUTHENTICATED,
    ) -> grpc.RpcMethodHandler:
        original_handler = await continuation(handler_call_details)

        async def abort_behavior(request_or_iterator, context):
            await context.abort(code, message)

        if original_handler is None:
            return grpc.unary_unary_rpc_method_handler(abort_behavior)

        return original_handler._replace(
            unary_unary=abort_behavior if original_handler.unary_unary else None,
            unary_stream=abort_behavior if original_handler.unary_stream else None,
            stream_unary=abort_behavior if original_handler.stream_unary else None,
            stream_stream=abort_behavior if original_handler.stream_stream else None,
        )

    _PUBLIC_METHODS = frozenset(
        {
            "/pelidum.services.grpc.mpac.MPAC/LoginUser",
            "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
            "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
        }
    )

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        if handler_call_details.method in self._PUBLIC_METHODS:
            return await continuation(handler_call_details)

        jwt_token = None
        api_key = None

        for key, value in handler_call_details.invocation_metadata:
            if key == "x-jwt-token":
                jwt_token = value
            elif key == "x-api-key":
                api_key = value

        if jwt_token:
            if not MPAC_JWT_SECRET:
                return await self._abort(
                    continuation, handler_call_details, "JWT auth not configured."
                )
            try:
                payload = pyjwt.decode(
                    jwt_token,
                    MPAC_JWT_SECRET,
                    algorithms=["HS256"],
                    audience=["mpac-web", "mpac-system"],
                )
                user_email = payload.get("sub")
                if not user_email:
                    return await self._abort(
                        continuation, handler_call_details, "JWT missing sub claim."
                    )
                WEB_UI_CONTEXT.set(True)
                USER_EMAIL_CONTEXT.set(user_email)
                logging.info(
                    f"AUTH: ok jwt user={user_email} method={handler_call_details.method}"
                )
                return await continuation(handler_call_details)
            except pyjwt.ExpiredSignatureError:
                logging.warning(
                    f"AUTH: fail jwt_expired method={handler_call_details.method}"
                )
                return await self._abort(
                    continuation, handler_call_details, "JWT expired."
                )
            except pyjwt.PyJWTError as e:
                logging.warning(
                    f"AUTH: fail jwt_invalid method={handler_call_details.method} detail={e}"
                )
                return await self._abort(
                    continuation, handler_call_details, "Invalid JWT."
                )

        if api_key:
            user_email = await self.key_lookup(api_key)
            if user_email:
                USER_EMAIL_CONTEXT.set(user_email)
                logging.info(
                    f"AUTH: ok api_key user={user_email} method={handler_call_details.method}"
                )
                return await continuation(handler_call_details)
            logging.warning(
                f"AUTH: fail api_key_invalid method={handler_call_details.method}"
            )
            return await self._abort(
                continuation, handler_call_details, "Invalid API key."
            )

        logging.warning(
            f"AUTH: fail no_credentials method={handler_call_details.method}"
        )
        return await self._abort(
            continuation, handler_call_details, "Missing credentials."
        )
