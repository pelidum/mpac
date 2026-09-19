import asyncio
import asyncpg
import grpc
import logging as stdlib_logging
import os
import signal
import traceback

from google.protobuf.internal import api_implementation

from absl import app, flags, logging
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from server import service_pb2
from server import service_pb2_grpc
from server.objects.auth import ApiAuthClient
from server.objects.db import MPAC_OBJECTS, DBMixin  # noqa: F401
from server.objects.inference import InferenceMixin
from server.objects.answers import AnswersMixin
from server.objects.attachments import AttachmentsMixin
from server.objects.backends import BackendsMixin
from server.objects.benchmarks import BenchmarksMixin
from server.objects.models import ModelsMixin
from server.objects.rate_limits import RateLimitsMixin
from server.objects.runs import RunsMixin
from server.objects.tests import TestsMixin
from server.objects.test_items import TestItemsMixin
from server.objects.users import UsersMixin

# Suppress annoying OpenAI logs
stdlib_logging.getLogger("httpx").setLevel(stdlib_logging.WARNING)

MPAC_MAX_MESSAGE_SIZE = int(
    os.getenv("MPAC_MAX_MESSAGE_SIZE", str(50 * 1024 * 1024))
)  # 50 MB default
STALE_RUN_SWEEP_INTERVAL_S = 120
SERVICE_NAMES = (
    service_pb2.DESCRIPTOR.services_by_name["MPAC"].full_name,
    health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
    reflection.SERVICE_NAME,
)

FLAGS = flags.FLAGS
flags.DEFINE_string("host", "0.0.0.0", "Host address for the server")
flags.DEFINE_integer("port", 50051, "Port number for the server")
flags.DEFINE_string(
    "admin_onboarding_id",
    None,
    "On initial server config, provide this flag to add the first admin user to bootstrap the server / invite other users.",
)
flags.DEFINE_string(
    "admin_onboarding_password",
    None,
    "On initial server config, provide this flag to add a corresponding password for the admin.",
)
flags.DEFINE_string(
    "admin_onboarding_api_key",
    None,
    "On initial server config, provide a raw API key to set for the admin user.",
)
flags.DEFINE_string(
    "db_host",
    "localhost",
    "The database connection hostname (postgres-compatible).",
)
flags.DEFINE_string(
    "db_user",
    "postgres",
    "The database connection username (postgres-compatible).",
)
flags.DEFINE_string(
    "db_password",
    None,
    "The database connection password (postgres-compatible).",
)
flags.DEFINE_integer(
    "db_pool_size",
    10,
    "The max number of DB connections per instance. Keep small for Cloud Run horizontal scaling.",
)


class MPAC(
    DBMixin,
    InferenceMixin,
    AnswersMixin,
    AttachmentsMixin,
    BackendsMixin,
    BenchmarksMixin,
    ModelsMixin,
    RateLimitsMixin,
    RunsMixin,
    TestsMixin,
    TestItemsMixin,
    UsersMixin,
    service_pb2_grpc.MPACServicer,
):
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool


async def serve(host: str, port: int) -> None:
    db_pool = await asyncpg.create_pool(
        host=FLAGS.db_host,
        user=FLAGS.db_user,
        password=os.getenv("MPAC_DB_PASSWORD") or FLAGS.db_password or None,
        database="mpac",
        min_size=2,
        max_size=FLAGS.db_pool_size,
    )

    api_key_interceptor = ApiAuthClient(db_pool=db_pool)

    server = grpc.aio.server(
        options=[
            ("grpc.max_send_message_length", MPAC_MAX_MESSAGE_SIZE),
            ("grpc.max_receive_message_length", MPAC_MAX_MESSAGE_SIZE),
        ],
        interceptors=[api_key_interceptor],
    )

    mpac_service = MPAC(db_pool=db_pool)

    await mpac_service.connect_db(
        admin_onboarding_id=os.getenv("MPAC_ADMIN_ONBOARDING_ID")
        or FLAGS.admin_onboarding_id
        or None,
        admin_onboarding_password=os.getenv("MPAC_ADMIN_ONBOARDING_PASSWORD")
        or FLAGS.admin_onboarding_password
        or None,
        admin_onboarding_api_key=os.getenv("MPAC_ADMIN_API_KEY")
        or FLAGS.admin_onboarding_api_key
        or None,
    )
    await mpac_service.resume_stale_benchmarks()
    await mpac_service.resume_stale_runs()

    async def _stale_run_sweeper() -> None:
        while True:
            await asyncio.sleep(STALE_RUN_SWEEP_INTERVAL_S)
            try:
                await mpac_service.resume_stale_runs()
            except Exception:
                logging.error("Stale run sweep failed:")
                logging.error(traceback.format_exc())

    sweeper_task = asyncio.create_task(_stale_run_sweeper(), name="stale-run-sweeper")

    service_pb2_grpc.add_MPACServicer_to_server(mpac_service, server)

    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    health_servicer.set(
        service_pb2.DESCRIPTOR.services_by_name["MPAC"].full_name,
        health_pb2.HealthCheckResponse.SERVING,
    )
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    listen_addr = f"{host}:{port}"
    server.add_insecure_port(listen_addr)
    logging.info(f"Starting MPAC gRPC API server on {listen_addr}")
    if os.getenv("MPAC_ENABLE_REFLECTION", "").lower() in ("1", "true"):
        reflection.enable_server_reflection(SERVICE_NAMES, server)
        logging.info("gRPC reflection enabled")
    await server.start()

    shutdown_event = asyncio.Event()

    def _handle_signal():
        logging.info("Received shutdown signal, draining...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    await shutdown_event.wait()

    health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
    health_servicer.set(
        service_pb2.DESCRIPTOR.services_by_name["MPAC"].full_name,
        health_pb2.HealthCheckResponse.NOT_SERVING,
    )
    sweeper_task.cancel()
    await server.stop(grace=25)
    await db_pool.close()


def main(argv):
    del argv
    asyncio.run(serve(FLAGS.host, FLAGS.port))


if __name__ == "__main__":
    app.run(main)
