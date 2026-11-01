import asyncio
import base64
import contextlib
import dataclasses
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import asyncpg
import grpc
import jwt
from grpc_health.v1 import health_pb2, health_pb2_grpc
from python.runfiles import runfiles

from proto import service_pb2

ADMIN_ID = "contract-admin@example.com"
ADMIN_PASSWORD = "contract-admin-password"
ADMIN_API_KEY = "sk_contract_admin_key"
JWT_SECRET = "contract-jwt-secret"
POSTGRES_IMAGE = "postgres:16"
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.50"
BLOB_BUCKET = "mpac"
STARTUP_TIMEOUT_S = 90
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MPAC_SERVICE = service_pb2.DESCRIPTOR.services_by_name["MPAC"]


@dataclasses.dataclass(frozen=True)
class Postgres:
    host: str
    port: int
    user: str
    password: str


@dataclasses.dataclass(frozen=True)
class BlobStore:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str = BLOB_BUCKET

    def env(self):
        return {
            "MPAC_BLOB_URL": f"s3://{self.bucket}",
            "AWS_ENDPOINT": self.endpoint,
            "AWS_ALLOW_HTTP": "true",
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": self.access_key,
            "AWS_SECRET_ACCESS_KEY": self.secret_key,
        }


class RpcRecorder(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor):
    def __init__(self):
        self.methods = set()

    def intercept_unary_unary(self, continuation, client_call_details, request):
        self.methods.add(client_call_details.method)
        return continuation(client_call_details, request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        self.methods.add(client_call_details.method)
        return continuation(client_call_details, request)


RECORDER = RpcRecorder()


def all_rpc_paths():
    return {
        f"/{MPAC_SERVICE.full_name}/{method.name}" for method in MPAC_SERVICE.methods
    }


def jwt_metadata(subject, audience="mpac-web", secret=JWT_SECRET, expires_in=600):
    claims = {"aud": audience, "exp": int(time.time()) + expires_in}
    if subject is not None:
        claims["sub"] = subject
    return (("x-jwt-token", jwt.encode(claims, secret, algorithm="HS256")),)


def onboarding_env():
    return {
        "MPAC_ADMIN_ONBOARDING_ID": ADMIN_ID,
        "MPAC_ADMIN_ONBOARDING_PASSWORD": ADMIN_PASSWORD,
        "MPAC_ADMIN_API_KEY": ADMIN_API_KEY,
    }


async def connect(pg, db_name="postgres"):
    return await asyncpg.connect(
        host=pg.host,
        port=pg.port,
        user=pg.user,
        password=pg.password,
        database=db_name,
    )


def _run_sql(pg, sql):
    async def run():
        conn = await connect(pg)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run())


def _wait_for_postgres(pg):
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while True:
        try:
            _run_sql(pg, "SELECT 1")
            return
        except (OSError, asyncpg.PostgresError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@contextlib.contextmanager
def postgres():
    if os.environ.get("MPAC_CONTRACT_PG_HOST"):
        yield Postgres(
            host=os.environ["MPAC_CONTRACT_PG_HOST"],
            port=int(os.environ.get("MPAC_CONTRACT_PG_PORT", "5432")),
            user=os.environ.get("MPAC_CONTRACT_PG_USER", "postgres"),
            password=os.environ.get("MPAC_CONTRACT_PG_PASSWORD", ""),
        )
        return
    password = uuid.uuid4().hex
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--publish",
            "127.0.0.1::5432",
            POSTGRES_IMAGE,
        ],
        text=True,
    ).strip()
    try:
        mapping = subprocess.check_output(
            ["docker", "port", container, "5432/tcp"], text=True
        )
        pg = Postgres(
            host="127.0.0.1",
            port=int(mapping.splitlines()[0].rsplit(":", 1)[1]),
            user="postgres",
            password=password,
        )
        _wait_for_postgres(pg)
        yield pg
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container], capture_output=True, check=False
        )


@contextlib.contextmanager
def database(pg):
    name = f"mpac_contract_{uuid.uuid4().hex[:12]}"
    _run_sql(pg, f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        _run_sql(pg, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _wait_for_http(url):
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while True:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _create_bucket(container, bucket):
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while True:
        shell = subprocess.run(
            ["docker", "exec", "-i", container, "weed", "shell"],
            input=f"s3.bucket.create -name {bucket}\nexit\n",
            capture_output=True,
            text=True,
            check=False,
        )
        listed = subprocess.run(
            ["docker", "exec", "-i", container, "weed", "shell"],
            input="s3.bucket.list\nexit\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if bucket in listed.stdout:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"cannot create bucket {bucket}: {shell.stdout}{shell.stderr}"
            )
        time.sleep(0.5)


@contextlib.contextmanager
def blob_store():
    access_key, secret_key = "mpac", uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as config_dir:
        os.chmod(config_dir, 0o755)
        config = os.path.join(config_dir, "s3.json")
        with open(config, "w") as f:
            json.dump(
                {
                    "identities": [
                        {
                            "name": "mpac",
                            "credentials": [
                                {"accessKey": access_key, "secretKey": secret_key}
                            ],
                            "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                        }
                    ]
                },
                f,
            )
        os.chmod(config, 0o644)
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--volume",
                f"{config_dir}:/etc/mpac:ro",
                "--publish",
                "127.0.0.1::8333",
                SEAWEEDFS_IMAGE,
                "server",
                "-s3",
                "-s3.config=/etc/mpac/s3.json",
                "-dir=/data",
                "-master.volumeSizeLimitMB=64",
            ],
            text=True,
        ).strip()
        try:
            mapping = subprocess.check_output(
                ["docker", "port", container, "8333/tcp"], text=True
            )
            endpoint = f"http://127.0.0.1:{mapping.splitlines()[0].rsplit(':', 1)[1]}"
            _wait_for_http(endpoint)
            _create_bucket(container, BLOB_BUCKET)
            yield BlobStore(endpoint, access_key, secret_key)
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container],
                capture_output=True,
                check=False,
            )


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(address, process):
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    with grpc.insecure_channel(address) as channel:
        health = health_pb2_grpc.HealthStub(channel)
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"server exited early with code {process.returncode}"
                )
            try:
                response = health.Check(health_pb2.HealthCheckRequest(), timeout=1)
                if response.status == health_pb2.HealthCheckResponse.SERVING:
                    return
            except grpc.RpcError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"server at {address} never became healthy")
            time.sleep(0.25)


def _log_path(name):
    directory = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR") or tempfile.gettempdir()
    return os.path.join(directory, f"{name}.log")


def runfile(env_var):
    return runfiles.Create().Rlocation(os.environ[env_var])


def db_flags(pg, db_name):
    return [
        f"--db_host={pg.host}",
        f"--db_port={pg.port}",
        f"--db_name={db_name}",
        f"--db_user={pg.user}",
    ]


def secret_key_url():
    path = os.path.join(tempfile.gettempdir(), f"mpac-contract-key-{os.getpid()}")
    if not os.path.exists(path):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as key_file:
            key_file.write(base64.b64encode(os.urandom(32)).decode())
    return f"file://{path}"


@contextlib.contextmanager
def server(
    pg,
    db_name,
    onboard=True,
    binary_env="MPAC_CONTRACT_SERVER_BIN",
    args_env="MPAC_CONTRACT_SERVER_ARGS",
    extra_env=None,
):
    binary = runfile(binary_env)
    port = _free_port()
    command = [
        binary,
        *os.environ.get(args_env, "").split(),
        "--host=127.0.0.1",
        f"--port={port}",
        *db_flags(pg, db_name),
    ]
    env = os.environ | {
        "MPAC_DB_PASSWORD": pg.password,
        "MPAC_JWT_SECRET": JWT_SECRET,
        "MPAC_SECRET_KEY_URL": secret_key_url(),
    }
    env |= extra_env or {}
    if onboard:
        env |= onboarding_env()
    log_name = (
        "server" if binary_env == "MPAC_CONTRACT_SERVER_BIN" else binary_env.lower()
    )
    extra_workers = int(os.environ.get("MPAC_CONTRACT_EXTRA_WORKERS", "0"))
    processes = []
    with contextlib.ExitStack() as logs:
        log = logs.enter_context(open(_log_path(log_name), "w"))
        processes.append(
            subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        )
        try:
            address = f"127.0.0.1:{port}"
            _wait_for_health(address, processes[0])
            for index in range(extra_workers):
                worker_port = _free_port()
                worker_log = logs.enter_context(open(_log_path(f"worker-{index}"), "w"))
                worker = subprocess.Popen(
                    [
                        binary,
                        "worker",
                        "--host=127.0.0.1",
                        f"--port={worker_port}",
                        "--skip_migrate",
                        *db_flags(pg, db_name),
                    ],
                    env=env,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(worker)
                _wait_for_health(f"127.0.0.1:{worker_port}", worker)
            yield address
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
