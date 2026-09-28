# MPAC

**Multi-Provider Agent Consensus** — a server + API + UI for building custom AI
evals and measuring model quality across providers.

MPAC lets you define **tests** (evaluation or survey), run them against one or
more **models** served by pluggable **backends** (vLLM, Ollama, OpenAI,
OpenRouter, llama.cpp), and compare results with metrics, leaderboards,
head-to-head **benchmarks**, and exportable PDF reports.

- **`server/`** — gRPC backend (Python + asyncpg + PostgreSQL). All domain
  objects are protobuf messages persisted as JSONB + canonical bytes.
- **`ui/`** — FastAPI, server-side-rendered web frontend (Jinja2 templates)
  that talks to the backend over gRPC.

The build is fully hermetic via [Bazel](https://bazel.build/) (Bzlmod). See
[`docs/mpac_guide.md`](docs/mpac_guide.md) for the full technical guide and
[`CLAUDE.md`](CLAUDE.md) for a concise structure/patterns overview.

## Prerequisites

- **Bazelisk** (pins Bazel `8.2.0` via `.bazelversion`) — `brew install bazelisk`
  or see the [Bazelisk releases](https://github.com/bazelbuild/bazelisk/releases).
- **Python 3.11** on the host (`pyenv install 3.11 && pyenv global 3.11`).
- **Docker** (only needed to build/run container images or the integration test DB).
- **PostgreSQL** (only for running the server; a container works — see below).

No manual `pip install` is required: Python dependencies are resolved
hermetically from `requirements_lock.txt` by Bazel.

## Build & test

```bash
# Build everything (both services + container images)
bazel build //...

# Run all unit tests
bazel test //server/... //ui/...

# Run a single test
bazel test //server/objects:users_test
```

## Run locally

**1. Start PostgreSQL** (any Postgres 16; Docker shown here):

```bash
docker run -d --name mpac-pg -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=mpac \
  postgres:16
```

**2. Start the gRPC server** (creates the schema and onboards a first admin):

```bash
bazel run //server:server -- \
  --db_host=localhost --db_user=postgres --db_password=postgres \
  --admin_onboarding_id=admin@example.com \
  --admin_onboarding_password=changeme \
  --port=50051
```

**3. Start the web UI** (connects to the server; then open http://localhost:8080):

```bash
MPAC_HOST=localhost MPAC_PORT=50051 MPAC_DEBUG=true \
MPAC_JWT_SECRET=dev-secret \
  bazel run //ui:mpac_ui
```

Use `bazel run //server:cli -- --help` for the command-line client.

### Key environment variables

| Variable | Component | Description |
|----------|-----------|-------------|
| `MPAC_JWT_SECRET` | server, ui | HS256 signing secret for JWTs |
| `MPAC_HOST` / `MPAC_PORT` | ui | gRPC server address (default `0.0.0.0:50051`) |
| `MPAC_DEBUG` | ui | Use an insecure gRPC channel for local dev |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ui | Optional Google OAuth login |

See [Appendix A of the guide](docs/mpac_guide.md#appendix-a-environment-variables)
for the complete list.

## Container images

Images are built with [`rules_oci`](https://github.com/bazel-contrib/rules_oci)
(no Dockerfiles) and published to the GitHub Container Registry:

```bash
# Load a locally-built image into Docker
bazel run //server:server_image_load
bazel run //ui:mpac_ui_image_load

# Push (CI does this on merge to main -> ghcr.io/pelidum/mpac_server, .../mpac_ui)
bazel run //server:server_image_push
bazel run //ui:mpac_ui_image_push
```

## Managing Python dependencies

Add or change a pinned version in `requirements.txt`, then regenerate the lock:

```bash
bazel run //:requirements.update
```

## Deployment

Self-hosting configurations live under [`terraform/`](terraform/), and each is a single
`terraform apply` after filling in a `terraform.tfvars`. See
[`terraform/DEPLOYMENT.md`](terraform/DEPLOYMENT.md) for a side-by-side comparison (cost, TLS,
security, operations) to help you choose.

- [`terraform/on_prem/`](terraform/on_prem/): a single machine running Docker (PostgreSQL,
  server, UI, Envoy TLS proxy). Also covered in the
  [technical guide](docs/mpac_guide.md#11-deployment-on-premises-docker-admin).
- [`terraform/gcloud/`](terraform/gcloud/): Google Cloud (Cloud Run + Cloud SQL). Scales to
  zero, roughly $13–15/month for light use.
- [`terraform/aws/`](terraform/aws/): AWS (ALB + Fargate + RDS). Always on, roughly
  $65/month.

## License

[MIT](LICENSE).
