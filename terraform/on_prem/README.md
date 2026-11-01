# MPAC on-premises

One Terraform apply deploys the Rust API, PostgreSQL, SeaweedFS, UI, TLS proxy,
and coordinated database/blob backups. Workers run in the API process by default.
Optional separate workers use the same private network. Inference services are
configured separately and are not installed by this deployment.

Only Envoy publishes ports. `/blobs/*` goes to SeaweedFS without changing the
signed host, object path, or query. All other HTTPS requests go to the UI, and the
separate TLS gRPC listener goes to the API.

## Local end-to-end quickstart

Requires Docker, Terraform >= 1.7, and Bazel for building the current development
images.

Public base-image pulls use a repository-owned anonymous registry config, so local
builds do not invoke credential helpers from your Docker config or require a
Google Cloud login. The first build still downloads the pinned public base images.

From the repository root:

```bash
bazel run //server/mpac:mpac_image_load_amd64
bazel run //ui:mpac_ui_image_load
cp terraform/on_prem/terraform.tfvars.example terraform/on_prem/terraform.tfvars
```

On arm64, use `mpac_image_load_arm64` and the `latest-arm64` tag instead.
Edit `terraform.tfvars`: set database/JWT secrets and the initial admin password.
Configure your existing inference backend in MPAC Admin after signing in.
Terraform does not install an inference service or download models.

```bash
terraform -chdir=terraform/on_prem init
terraform -chdir=terraform/on_prem apply \
  -var='use_local_images=true' \
  -var='server_image=ghcr.io/pelidum/mpac_server:latest-amd64'
```

Use these same overrides on subsequent applies, or put them in your tfvars.
For ports already in use, set `envoy_http_port = 8080`,
`envoy_https_port = 8443`, and `envoy_grpc_port = 50052`, for example.
`envoy_bind_ip = "127.0.0.1"` restricts the deployment to this machine.

Open the `terraform output ui_url` address, accept the generated self-signed
certificate, and sign in with the onboarding admin credentials. The same admin
settings can remain configured; an existing admin is not overwritten.

Run the smoke check against the deployed stack:

```bash
export MPAC_SMOKE_PASSWORD='your-onboarding-admin-password'
bazel run //terraform/on_prem:smoke -- \
  --url=https://localhost \
  --grpc=localhost:50051 \
  --ca-cert="$PWD/terraform/on_prem/generated/envoy.crt" \
  --admin-email=admin@example.com
```

Adjust the URL and gRPC port for your configuration. For a longer check, `bazel run
//terraform/on_prem:soak` also tests a multi-model local run, key decryption, cancellation, a server
kill, live benchmark updates and a 2 GiB upload. It needs `--server-container`, `--ui-container` and
`--network`, and it runs a scripted provider in a container on that network. Alternatively, use
`--password-file=/absolute/path/to/a/private/password-file`. The check signs in
through the UI, creates an image attachment through the UI upload flow, downloads
it through Envoy, exports its ZIP through the UI's private blob transport, and
optionally runs a separate text evaluation against a configured backend when
`--backend-url` and `--model` are supplied. Records remain in the UI for
inspection. Without those options, it tests login and attachments only.

To import the bundled test datasets with the CLI, use an API key for your admin
and trust the same certificate for both gRPC and signed HTTPS uploads:

```bash
bazel run //cli -- \
  --mpac_api_key="$MPAC_API_KEY" --bootstrap --secure_channel \
  --mpac_cert_file="$PWD/terraform/on_prem/generated/envoy.crt"
```

For custom gRPC ports, also pass `--mpac_server_port`. Required attachment failures
make bootstrap fail instead of importing those items without their attachments.

Both local images must be loaded before applying. Terraform detects rebuilt
local tags by image ID. `use_local_images = false` instead resolves registry
image digests for the default `latest` tags.

## Storage and signed URLs

- PostgreSQL: `postgres_data_path`, default `/var/lib/mpac/postgres`.
- SeaweedFS: `blob_data_path`, default `/var/lib/mpac/seaweedfs`. Volumes, master
  state, and filer metadata all persist under this directory.
- Backups: `postgres_backup_path`, default `/var/lib/mpac/backups`.

Terraform generates S3 credentials with `random_password`, writes the identity
file under `generated/`, and creates the `blobs` bucket before starting the API.
S3 keys and secrets are not published as outputs. Terraform state contains
secrets and must be protected along with tfvars and `generated/`. Run
`chmod 600 terraform.tfvars terraform.tfstate` after the first apply. The backup
directory is `0700` and its files are `0600`, so only root can read them on the host.

The server and workers use `s3://blobs` at `http://seaweedfs:8333`. Download and
upload URLs name the public HTTPS origin, including its configured port. The UI
uses `MPAC_BLOB_INTERNAL_ENDPOINT` to connect privately while preserving the
signed `Host`, path, and query, so `localhost` URLs and self-signed certificates
work without disabling TLS verification or adding a container DNS workaround.
Browser downloads still go through the public TLS listener.

The database and bucket belong together. Do not share the same bucket prefixes
between independent MPAC databases: attachment maintenance removes old
unreferenced objects. New uploads default to a 2 GiB storage limit
(`max_attachment_size_mb`). The browser streams each file through the UI, and rows
hold only metadata. Inference accepts blob-backed attachments up to 64 MiB; a
larger attachment produces a `BAD_REQUEST` answer for that item.

## Provider key encryption

MPAC encrypts provider API keys before they reach the database. Terraform generates
a master key at `generated/secrets/master.key` and mounts it read-only into the
server and worker containers only. Terraform state also holds this key.

Set `secret_key_path` to use your own file (32 random bytes, base64 encoded), or
`secret_key_url` and `secret_key_vault_token` to use a Vault or OpenBao transit key,
ideally on another host. This protects against database, dump and SQL read leaks.
It does not protect against root on this host. Back up the key separately from the
dumps. See [the technical guide](../../docs/mpac_guide.md#1110-master-key-operations)
for rotation and recovery.

## Worker modes

```hcl
worker_replicas    = 0 # API + embedded worker (default)
worker_concurrency = 32 # per worker
```

Set `worker_replicas = 2` for an API-only server and two worker-only containers.
Workers share the database, blob credentials, and JWT configuration. They have
no published ports and can reach host inference services through
`host.docker.internal`. Containers have 35 seconds to stop, covering the worker
shutdown grace period.

Use your existing host inference service, such as Ollama or LM Studio. Register
`http://host.docker.internal:11434/v1` or `http://host.docker.internal:1234/v1`
in Admin with **Local backend** enabled. The host service must listen on an
interface reachable from Docker. For the optional inference smoke check, pass
`--backend-url=http://host.docker.internal:11434/v1 --model=<existing-model>`.
The backend must already be registered in MPAC; the smoke check does not install
models or register services.

## Backups and restore

The backup container obtains the attachment lifecycle lock, runs `pg_dump`,
copies immutable permanent blobs with rclone, and only then publishes the dump.
Publication, deletion, migration, and blob cleanup pause during this operation;
reads and unrelated work can continue. A failed dump or copy does not publish a
completed backup, and releasing the connection releases the lock.

`backup_interval_seconds` defaults to 86400, with an initial backup at startup.
`postgres_backup_retention_days` defaults to 30. The legacy
`postgres_backup_schedule` input is retained for compatibility; scheduling uses
the interval rather than cron. Blob backups are append-only so older retained
dumps do not lose their referenced objects. Blob backup pruning is manual.
The rclone binary comes from a pinned container image; startup does not install
packages from the network.

A backup contains `mpac_<UTC timestamp>.dump` and a shared `blobs/` directory.
For restore, stop the API, UI, and any separate worker containers first. With
default names/database settings:

```bash
docker stop mpac-server mpac-ui
# Also stop any mpac-worker-* containers.
docker exec mpac-postgres-backup /tools/rclone copy /backups/blobs :s3:blobs/blobs
docker exec mpac-postgres-backup pg_restore -d mpac --clean --if-exists \
  /backups/mpac_20261031_120000.dump
docker start mpac-server mpac-ui
# Restart separate workers if configured.
```

## TLS, ports and configuration

Terraform generates `generated/envoy.crt` and `generated/envoy.key` by default.
`tls_domain` sets the public hostname; the certificate also covers `localhost`
and `127.0.0.1`. Supply both `tls_cert_path` and `tls_key_path` to use an existing
certificate. Clients and the smoke check should trust the configured certificate.

| Service | Container port | Published port |
|---|---|---|
| UI | 8080 | through Envoy |
| API | `server_port` (50051) | through Envoy |
| PostgreSQL | 5432 | none |
| SeaweedFS S3 | 8333 | none |
| Envoy HTTP | 8080 | `envoy_http_port` (80) |
| Envoy HTTPS | 8443 | `envoy_https_port` (443) |
| Envoy gRPC TLS | 50051 | `envoy_grpc_port` (50051) |

The default Envoy config is rendered to `generated/envoy.yaml`. Set
`envoy_config_path` to supply your own, retaining the signed blob route and
correct ports. Config/certificate changes restart Envoy, and credential-file
changes restart SeaweedFS. Google OAuth remains optional; see
[the deployment guide](../DEPLOYMENT.md#google-sign-in-optional).

## Validation and operations

```bash
terraform -chdir=terraform/on_prem validate
terraform -chdir=terraform/on_prem test  # mocked providers; no containers created
bazel test //ui:test_attachments

docker logs mpac-server
docker logs mpac-postgres-backup
docker exec -it mpac-postgres psql -U postgres -d mpac
terraform -chdir=terraform/on_prem destroy
```

Terraform destroy removes containers, network and managed volumes; database,
blob and backup host directories remain. Keep production data outside the
Terraform module directory so recursive tools do not traverse database files.
