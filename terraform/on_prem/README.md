# MPAC On-Prem (Terraform)

Spin up a complete, self-hosted MPAC stack — PostgreSQL, gRPC server, FastAPI UI, and an
Envoy TLS proxy — on a single machine with **one `terraform apply`**. No shell scripts, no
manual certificate generation: Terraform pulls the public images, generates a self-signed
certificate, and wires everything together.

## Architecture

```
Internet → Envoy Proxy (TLS termination) — ONLY PUBLIC ENTRY POINT
           ├─ HTTP (80) → HTTPS redirect
           ├─ HTTPS (443) → FastAPI UI (8080) [internal only]
           └─ gRPC (50051) → MPAC Server (50051) [internal only]
                             └─ PostgreSQL (5432) [internal only]
```

- PostgreSQL, Server, and UI have **no** external port exposure.
- All backend services are reachable only within the Docker network.
- Envoy is the single point of entry, terminating TLS for the UI and gRPC API.

Containers communicate over a Docker network using hostnames: `postgres`, `server`, `ui`, `envoy`.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.0
- Docker running locally (the Terraform Docker provider talks to your local daemon)
- Ports 80, 443, and 50051 free on the host

Images are pulled from GitHub Container Registry (`ghcr.io/pelidum/mpac_server` and
`ghcr.io/pelidum/mpac_ui`), which is public — no registry login required.

## Quick Start

```bash
cd terraform/on_prem

# 1. Provide your secrets
cp terraform.tfvars.example terraform.tfvars
#    edit terraform.tfvars — at minimum set postgres_password and mpac_jwt_secret

# 2. Deploy
terraform init
terraform apply
```

That's it. On the first `apply`, Terraform:

1. Generates a self-signed TLS certificate into `generated/envoy.crt` and `generated/envoy.key`.
2. Pulls the Postgres, MPAC server, MPAC UI, and Envoy images.
3. Starts all containers on a private Docker network.

Then open **https://localhost/** (accept the self-signed certificate warning).

### Bootstrapping the first admin

Set these in `terraform.tfvars` before your first `apply` to create an initial admin account:

```hcl
server_admin_onboarding_id       = "admin@example.com"
server_admin_onboarding_password = "your-secure-password"
```

The server **skips onboarding once an admin already exists**, so these are safe to leave set
after the first deploy — no follow-up apply or flag flip is needed.

## TLS

By default a self-signed certificate is generated in pure Terraform (via the `hashicorp/tls`
provider) and written to `generated/`. `tls_domain` (default `localhost`) sets the certificate's
common name and DNS SAN; `127.0.0.1` is always included as an IP SAN.

To **bring your own certificate** instead, set both paths in `terraform.tfvars` — generation is
skipped automatically:

```hcl
tls_cert_path = "/path/to/your/cert.pem"
tls_key_path  = "/path/to/your/key.pem"
```

## Files

- `main.tf` — network, containers, and TLS certificate generation
- `variables.tf` — variable declarations and defaults
- `envoy.yaml` — Envoy proxy configuration (mounted read-only into the container)
- `terraform.tfvars.example` — copy to `terraform.tfvars` and fill in
- `generated/` — auto-generated certs (git-ignored)

## Ports

Only Envoy ports are exposed externally; all backends are network-internal.

| Service    | Internal | External | Purpose                     |
|------------|----------|----------|-----------------------------|
| PostgreSQL | 5432     | none     | Database (network only)     |
| MPAC Server| 50051    | none     | gRPC (via Envoy only)       |
| MPAC UI    | 8080     | none     | Web UI (via Envoy only)     |
| Envoy      | 8080     | 80       | HTTP → HTTPS redirect       |
| Envoy      | 8443     | 443      | HTTPS UI access             |
| Envoy      | 50051    | 50051    | gRPC API (TLS)              |

Backend services are completely isolated from external access — all traffic goes through
Envoy's TLS-terminated endpoints.

## Secrets

Sensitive values live in `terraform.tfvars` (git-ignored):

- `postgres_password`
- `mpac_jwt_secret`
- `google_oauth_client_secret` (optional)
- `server_admin_onboarding_password` (bootstrap only)

For CI or shared hosts, prefer environment variables:

```bash
export TF_VAR_postgres_password="secure-password"
export TF_VAR_mpac_jwt_secret="secure-random-secret"
```

## Database Backups

A backup container runs `pg_dump` on a schedule and writes compressed dumps to
`postgres_backup_path` (default `/var/lib/mpac/backups`). Old dumps are pruned after
`postgres_backup_retention_days` (default 30). Configure in `terraform.tfvars`:

```hcl
postgres_backup_path           = "/var/lib/mpac/backups"
postgres_backup_retention_days = 30
postgres_backup_schedule       = "0 2 * * *"
```

### Manual backup

```bash
docker exec mpac-postgres pg_dump -U postgres -d mpac -Fc > mpac_$(date +%Y%m%d).dump
```

### Restore from backup

```bash
# Stop services that connect to the database
docker stop mpac-server mpac-ui

# Restore (drop + recreate)
docker exec -i mpac-postgres pg_restore -U postgres -d mpac --clean --if-exists \
  < /var/lib/mpac/backups/mpac_20260709_020000.dump

# Restart services
docker start mpac-server mpac-ui
```

## Useful Commands

```bash
# View logs
docker logs mpac-server -f
docker logs mpac-ui -f
docker logs mpac-envoy -f

# Connect to the database
docker exec -it mpac-postgres psql -U postgres -d mpac

# Test endpoints
curl http://localhost/       # redirects to HTTPS
curl -k https://localhost/   # UI over the self-signed cert

# Tear everything down
terraform destroy
```

## Troubleshooting

### Can't connect to the database directly
Intentional — PostgreSQL has no external ports. Use the container:
```bash
docker exec -it mpac-postgres psql -U postgres -d mpac
```

### Envoy can't reach services
Verify all containers share the network:
```bash
docker network inspect mpac-network
```

### Certificate warnings in the browser
Expected with the default self-signed certificate. Accept the warning, or bring your own cert
via `tls_cert_path` / `tls_key_path`.

### gRPC connection issues
Verify the server is listening on `0.0.0.0:50051` and check `docker logs mpac-envoy`.
