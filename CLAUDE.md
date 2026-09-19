# CLAUDE.md

## Build & Test

- **Always use bazel** for building and running tests. Never invoke pytest directly.
- Run a single test: `bazel test //server/objects:users_test`
- Run all tests in a package: `bazel test //server/objects:all`
- Run all MPAC tests: `bazel test //server/... //ui/...`

## Repository Structure

This repo is the standalone, open-source home of MPAC (Multi-Provider Agent
Consensus). The two services live at the repo root under `server/` and `ui/`.

### `server/`

gRPC backend service for MPAC.

- `service.proto` - protobuf definitions for all messages and RPCs
- `server.py` - gRPC server entrypoint; flags for DB, admin onboarding, ports
- `cli.py` - CLI client for the gRPC API
- `objects/` - RPC handler mixins, one per domain:
  - `db.py` - database pool init, schema migrations, onboarding, generic CRUD helpers (`_grpc_get`, `_grpc_create`, `_grpc_update`, `_grpc_delete`, `_grpc_list`), visibility clause
  - `auth.py` - API key and JWT auth interceptor; `USER_EMAIL_CONTEXT` contextvar
  - `users.py` - GetUser, CreateUser, UpdateUser, DeleteUser, ListUsers, LoginUser (password verify). GetUser/ListUsers strip `password_hash` and `api_key.hash` before returning
  - `tests.py` / `test_items.py` - test and test item CRUD
  - `runs.py` - test run lifecycle
  - `answers.py` - test run answer storage and retrieval
  - `attachments.py` - file attachment CRUD with modality enum (Audio=1, Image=2, Video=3, Text=4)
  - `backends.py` - inference backend registration
  - `models.py` - model registry
  - `benchmarks.py` - benchmark orchestration
  - `inference.py` - inference dispatch to backends
  - `rate_limits.py` - per-user rate limiting (TPM windows)
  - `backend_pool.py` - backend connection pooling
- `test_db/` - test database fixtures
- Each `objects/*.py` has a corresponding `*_test.py`

Python imports use `server` as the package root (e.g. `from server import service_pb2`,
`from server.objects.db import ...`). Note: the proto `package` is intentionally kept as
`pelidum.services.grpc.mpac` to preserve wire compatibility and stored `proto_bytes`; this
does not affect the Python import path (which derives from the Bazel package location).

### `ui/`

FastAPI web frontend for MPAC. Server-side rendered with Jinja2 templates.

- `server.py` - FastAPI app setup, middleware, template config
- `auth.py` - login routes (password and OAuth/Google), JWT cookie management, rate limiting
- `grpc_client.py` - gRPC stub factory for connecting to the backend
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

The UI imports the generated gRPC stubs from the backend (`from server import service_pb2`)
and depends on the `//server:service_py_proto` and `//server:mpac_proto_json` Bazel targets.

### Deployment / infrastructure

Terraform/IaC has **not** yet been migrated into this repo. It is planned as a follow-up.

## Database

PostgreSQL via asyncpg. Each domain object is stored as a row with:
- `id` (text PK)
- `proto_jsonb` (JSONB - for queries/filtering)
- `proto_bytes` (bytea - canonical protobuf serialization)

## Key Patterns

- All domain objects are protobuf messages, serialized to both JSON and bytes for storage
- `MessageToDict` / `MessageToJson` with `preserving_proto_field_name=True` and `always_print_fields_with_no_presence=True`
- Visibility/access control via `_visibility_where_clause` in db.py
- `USER_EMAIL_CONTEXT` contextvar set by auth interceptor, used by CRUD helpers for ownership checks
- PDF generation via WeasyPrint from Jinja2 templates; images embedded as data URIs
