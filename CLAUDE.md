# CLAUDE.md

## Build & Test

- **Always use bazel** for building and running tests. Never invoke pytest directly.
- Run a single test: `bazel test //server/mpac_queue:queue_db_test`
- Run all tests in a package: `bazel test //ui:all`
- Run all MPAC tests: `bazel test //server/... //proto/... //cli/... //ui/... //tests/...`
- Rust needs no local toolchain. Bazel fetches rustc/cargo (`rules_rust`) and a zig-based C
  toolchain (`hermetic_cc_toolchain`).
  - Format with `bazel run @rules_rust//:rustfmt -- //server/...`.
  - After editing any `server/**/Cargo.toml` dependency, repin with
    `CARGO_BAZEL_REPIN=1 bazel build //server/...`.
  - Clippy (`-Dwarnings`, pedantic) and rustfmt run as tests in each Rust package.
  - Some Rust tests need Postgres (`rust_db_test` targets such as
    `//server/mpac_queue:queue_db_test`). They use Docker `postgres:16`, or `MPAC_TEST_PG_*` if set.
- Contract suite: `bazel test //tests/contract:contract_rust_test` (`mpac serve` with the embedded worker and SeaweedFS blob storage),
  `:contract_rust_inline_test` (no blob storage, attachments stored inline) and
  `:contract_rust_split_test` (API-only server plus 3 separate `mpac worker` processes, with
  blobs). Blob targets also pull `chrislusf/seaweedfs:4.50`. It black-boxes every gRPC
  RPC against a real server and Postgres, and needs Docker locally (`postgres:16`) or
  `MPAC_CONTRACT_PG_{HOST,PORT,USER,PASSWORD}`. Any behavior change to the API must update it.
- Regenerate fixtures after changing `service.proto`:
  `bazel run //server/mpac_proto/testdata:generate_golden`. Auth fixtures (only when the werkzeug
  or PyJWT behavior changes): `bazel run //server/mpac_auth/testdata:generate_fixtures`.
- Rust images: `bazel build --config=release //server/mpac:mpac_image_index` (amd64 + arm64; release
  = opt + fat LTO + 1 codegen unit). Load one arch locally with
  `bazel run --config=release //server/mpac:mpac_image_load_amd64`.

## Repository Structure

This repo is the standalone, open-source home of MPAC (Multi-Provider Agent
Consensus). The backend is in `server/`, the web UI in `ui/`, the API definitions in `proto/`, and
the Python CLI client in `cli/`.

### `proto/`

The API definitions. Python imports use `proto` as the package root (e.g.
`from proto import service_pb2`).

- `service.proto` - protobuf definitions for all messages and RPCs
- `jobs.proto` - internal job-queue payloads (`Job` oneof) stored in `jobs.payload`. Never exposed
  over gRPC.

The proto `package` is intentionally `pelidum.services.grpc.mpac`. This does not affect the Python
import path, which derives from the Bazel package location.

### `cli/`

- `mpac_cli.py` - CLI client for the gRPC API, including `--bootstrap` for the datasets in `test_db/`.
  Run it with `bazel run //cli -- --help`.
- `test_db/` - test datasets and attachment fixtures

### `ui/`

FastAPI web frontend for MPAC. Server-side rendered with Jinja2 templates.

- `server.py` - FastAPI app setup, middleware, template config
- `auth.py` - login routes (password and OAuth/Google), JWT cookie management, rate limiting
- `grpc_client.py` - gRPC stub factory for connecting to the backend
- `blobs.py` - attachment upload (`CreateAttachmentUpload` → PUT → `FinalizeAttachment`, falling
  back to inline `CreateAttachment`) and content reads (`uri` or legacy `file`)
- `dependencies.py` - FastAPI dependency injection (current user, JWT token)
- `rate_limit.py` - login attempt rate limiter
- `csrf.py` - CSRF protection
- `routers/` - route handlers:
  - `home.py`, `tests.py`, `runs.py`, `models.py`, `benchmarks.py`, `settings.py`, `admin.py`, `attachments.py`, `metrics.py`, `help.py`
- `templates/` - Jinja2 HTML templates:
  - `base.html` - layout shell with CSS variables, nav, dark mode
  - `create_test.html` - test creation/editing with inline attachment previews, spreadsheet-style item table
  - `run_details.html` - run results page with spreadsheet answers table (frozen columns, sticky headers), model cards, leaderboard chart, confusion matrix, timeline
  - `run_report_pdf.html` - PDF export via WeasyPrint. A4 portrait, 15mm side margins. Spreadsheet answers table, model legend, error analysis section
  - Other pages: tests list, runs list, models, benchmarks, settings, admin, login
- `static/css/style.css` - CSS variables: `--color-bg-primary`, `--color-accent-primary: #cca139`, etc.

The UI imports the generated gRPC stubs from the backend (`from proto import service_pb2`)
and depends on the `//proto:service_py_proto` and `//proto:mpac_proto_json` Bazel targets.

### `server/`

The Rust backend.

- `Cargo.toml` / `Cargo.lock` - cargo workspace. Bazel reads them via `crate_universe`
  (`@crates//:<name>`).
- `BUILD` - `//server:lints` (shared clippy/rustc lint levels) and the image platforms
  `//server:linux_{amd64,arm64}_gnu`. These are glibc 2.28, so they select the zig toolchain.
- `rustfmt.toml`, `clippy.toml` - wired in through `.bazelrc`.
- `mpac/` - the `mpac` binary (clap; flags and env names are in `src/args.rs`).
  Subcommands: `serve` (the image entrypoint, so bare flags work; `--roles=api,worker` by
  default, i.e. API + embedded inference worker), `worker` (worker only, still serves gRPC
  health), `migrate` and `onboard`. Blob storage is opt-in via `--blob_url` /
  `MPAC_BLOB_URL` (`s3://bucket` with `AWS_*` env, or `gs://bucket`), plus
  `MPAC_BLOB_PUBLIC_ENDPOINT` when signed URLs must name a different host.
  Plus its OCI image targets (`gcr.io/distroless/cc-debian12`, published as `ghcr.io/pelidum/mpac_server:latest`).
- `mpac_proto/` - prost/tonic types generated from `proto/service.proto` (Bazel `protoc`), the
  embedded `DESCRIPTOR_POOL`, and `json::to_jsonb`, which writes `proto_jsonb` text identical to
  Python's `MessageToJson(preserving_proto_field_name=True,
  always_print_fields_with_no_presence=True)`. `testdata/` holds the Python-generated golden
  file that proves it.
- `mpac_api/` - the tonic `MPAC` service, one module per domain
  (`users`, `test_defs` = tests, `test_items`, `attachments`, `backends`, `models` +
  `model_catalog` + `backend_urls`, `runs`, `answers`, `benchmarks`). `auth.rs` is the async
  tower auth layer, `server.rs` handles health/reflection/drain, and `service.rs` is the trait
  impl that delegates to the modules.
- `mpac_blob/` - `object_store` wrapper: S3/GCS from one URL, signed GET/PUT URLs, streaming
  `inspect` (sha256 + first 8 KB + UTF-8 validation), the attachment sniffer (`sniff`, port of
  `attachment_parser`) and a byte-bounded `BlobCache`. Attachment rows store the object key in
  `uri` (`blobs/<sha256>`, or `tmp/<id>` while PENDING). API responses replace it with a
  signed URL or leave it empty.
- `mpac_inference/` - port of `inference.py`/`confidence.py`/`_validate_answer`: an
  OpenAI-compatible streaming client with the openai-SDK retry policy and Python-identical error
  strings, `answer::Engine` (answer one item), plus `difflib`/`pystr` helpers that reproduce
  Python semantics. `testdata/` holds fixtures generated from the retired Python server. They are frozen: no generator remains.
- `mpac_worker/` - job handlers (`answer_item`, `finalize_run` + `metrics`, `benchmark_step`),
  run creation and benchmark cancellation shared with the API (`runs`), blob cleanup and
  abandoned-upload pruning (`blobs`), and the worker loop
  (`worker`: claim, lease extension, cancellation, reaping, pruning, lease release on
  shutdown). Handlers that write commit their output and the job's completion in one
  lease-checked transaction (`fenced`). DB tests: `//server/mpac_worker:worker_db_test`.
- `mpac_secrets/` - provider key encryption: a data key per secret, a master key from a file, Vault/OpenBao
  transit, AWS KMS or Google Cloud KMS (`MPAC_SECRET_KEY_URL`, no cloud SDKs), a 5-minute plaintext cache, and the `secret_audit`
  trail. DB tests: `//server/mpac_secrets:secrets_db_test`. Pass
  `MPAC_TEST_VAULT_URL` or `MPAC_TEST_AWS_KMS_URL` with `--test_env` to test a real service.
- `mpac_queue/` - the PostgreSQL job queue: enqueue/claim (advisory-locked, per-backend
  capacity)/complete/fail/reschedule/reap/cancel, run completion accounting (`run_counters`,
  exactly one complete-run `finalize_run`, plus one per responder), a one-model-at-a-time
  barrier on `is_local` backends, least-in-flight fairness across backends, and `signals` (LISTEN/NOTIFY on `mpac_jobs`, `mpac_cancel`,
  `mpac_backend_changed`, with a polling fallback).
- `mpac_testing/` - `TestDb` for Rust tests that need Postgres (see `rust_db_test` in
  `tests/defs.bzl`).
- `mpac_http/` - shared reqwest client factory (rustls + ring provider, OS trust store).
- `mpac_db/` - sqlx Postgres pool config, embedded forward-only migrations in `migrations/`
  (`0001_baseline.sql` = the domain tables, `0002_jobs.sql` = the job queue and
  `run_counters`, `0003_jobs_seq.sql` = FIFO ordering, `0004_benchmark_runs.sql` = bracket state, `0005_jobs_lease_check.sql` = strict lease/status constraint, `0006_blob_cleanup.sql` = durable blob deletion, `0007_jobs_model_id.sql` = the model each job needs, `0008_backend_secrets.sql` and `0009_secret_audit.sql` = encrypted provider keys, `0010_drop_heartbeat.sql`), `onboarding`, `crud` (generic get/create/update/delete/list + visibility
  clause, ported from `db.py`) and `rate_limits` (spend/TPM/projected cost). Never edit an applied migration; add a new numbered file.
- `mpac_auth/` - parity with Python auth: werkzeug password hashes (`password`), API key SHA-256
  (`api_key`), and HS256 JWT verification with the `mpac-web`/`mpac-system` audiences (`jwt`).

### `tests/schema/`

`schema_test` (migrations, idempotency, concurrency and queue constraints), `onboard_test`
(onboarding creates a working admin) and `blob_config_test` (full attachment reads fail clearly
without blob configuration).
Same Postgres requirements as the contract suite: `bazel test //tests/schema:all`.

### `tests/resilience/`

`resilience_test` runs real `mpac` processes against a scripted provider (`fake_provider.py`): worker
kill with no duplicate answers, SIGTERM handoff, a request longer than the lease, fragmented streams,
active cancellation, reflection toggling, and peak memory with large attachments. It takes about 4
minutes. Same Postgres requirements as the contract suite.

### `tests/contract/`

Black-box gRPC contract suite (pytest). `harness.py` provides Postgres (env or Docker), creates a
throwaway database, and launches the server binary named by `MPAC_CONTRACT_SERVER_BIN`.
`conftest.py` holds the fixtures: admin (API key), member and outsider (JWT), and a debug backend.
`test_zz_rpc_coverage.py` fails if any MPAC RPC goes unexercised.

`third_party/hermetic_cc_toolchain/` holds a patch that drops `-Wl,--fix-cortex-a53-843419`, an
arg zig rejects, so that aarch64 can be cross-linked.

### Deployment (`terraform/`)

Infrastructure-as-code for self-hosting MPAC.

- `terraform/on_prem/` — single-machine Docker deployment (PostgreSQL, gRPC server, FastAPI
  UI, Envoy TLS proxy). Designed for a one-command `terraform init && terraform apply`: it
  pulls the public GHCR images (`ghcr.io/pelidum/mpac_server`, `ghcr.io/pelidum/mpac_ui`),
  generates a self-signed TLS cert in pure Terraform (with a bring-your-own override), and
  wires the containers together. Copy `terraform.tfvars.example` → `terraform.tfvars` first.
  Uses the `kreuzwerker/docker`, `hashicorp/tls`, and `hashicorp/local` providers.
- `terraform/gcloud/` — Google Cloud deployment (Cloud Run server + UI, Cloud SQL PostgreSQL 16,
  Secret Manager). A single flat root driven by `terraform.tfvars` (only `project_id` and
  `region` required). It enables its own APIs and generates the DB password and JWT secret.
  Deliberate choices, each documented in comments, that should not be "fixed":
  `edition = "ENTERPRISE"` (PG16+ defaults to Enterprise Plus, which has no db-f1-micro);
  Cloud SQL public IP with no authorized networks + client-cert-only (avoids VPC cost);
  server `cpu_idle = false` (background inference); both services public with app-layer auth;
  `public_access_mode` (`invoker_iam_disabled` default vs `allusers` + `drs_tag_value`) to
  work under Domain Restricted Sharing. Offline tests with mocked providers:
  `terraform init -backend=false && terraform test` in `terraform/gcloud/`.
- `terraform/aws/` — AWS deployment (ALB → one Fargate task running server + UI → RDS
  PostgreSQL 16 db.t4g.micro). Flat root, only `region` required. Deliberate choices: no NAT
  gateway (the task has a public IP for egress; its SG admits only the ALB); UI→server over
  localhost plaintext (`MPAC_GRPC_INSECURE`, as on-prem); ALB listeners mirror on-prem Envoy
  (80 redirect, 443 UI, 50051 gRPC); TLS modes self-signed (default) / Route 53 + ACM / BYO ACM
  ARN; secrets in SSM SecureString via ephemeral values + write-only args (never in state),
  rotated by bumping `secrets_version`; RDS `rds.force_ssl` + server `PGSSLMODE=require`;
  x86_64 only (images aren't multi-arch). Offline tests: `terraform test` in `terraform/aws/`
  (mocked provider, `apply` runs).
- `terraform/DEPLOYMENT.md` — comparison table and cross-target instructions. Update it when a
  target's cost, TLS, security or operations change.

## Database

PostgreSQL via sqlx. The schema lives in `server/mpac_db/migrations/` and is applied by
`mpac migrate` or at server startup. Each domain
object is stored as a row with:
- `id` (text PK)
- `proto_jsonb` (JSONB - for queries/filtering)
- `proto_bytes` (bytea - canonical protobuf serialization)

## Key Patterns

- All domain objects are protobuf messages, serialized to both JSON and bytes for storage
- `MessageToDict` / `MessageToJson` with `preserving_proto_field_name=True` and `always_print_fields_with_no_presence=True`
- Visibility/access control via `visibility_clause` in `server/mpac_db/src/crud.rs`
- The auth layer attaches a `Caller` to each request, used by CRUD helpers for ownership checks
- PDF generation via WeasyPrint from Jinja2 templates; images embedded as data URIs
