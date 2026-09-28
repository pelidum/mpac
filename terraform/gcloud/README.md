# MPAC on Google Cloud (Terraform)

Run MPAC on Google Cloud with **one `terraform apply`**: Cloud Run for the gRPC server and
web UI, and Cloud SQL for PostgreSQL. Both services scale to zero when idle, so a lightly used
instance costs **roughly $13–15/month** (plus whatever you spend on model inference).

## Architecture

```
Internet ─ HTTPS ─► Cloud Run: UI (FastAPI)              scale-to-zero, request-billed
                        │  gRPC over TLS (public URL, MPAC JWT)
Internet ─ HTTPS ─► Cloud Run: server (gRPC, h2c)        scale-to-zero, instance-billed
                        │  /cloudsql unix socket (Cloud SQL Auth Proxy, IAM + mTLS)
                    Cloud SQL: PostgreSQL 16             db-f1-micro, no authorized networks
```

- Secrets (DB password, JWT secret, OAuth secret, admin password) live in **Secret Manager**
  and are injected at runtime. Each service account can read only the secrets it needs.
- The server and UI run as **separate service accounts**, and only the server can reach the database.
- Both services are public and authenticate requests themselves (password/OAuth login → JWT;
  API keys for gRPC clients).
- Inference backends are registered after deploying through the MPAC API/UI (`CreateBackend`).
  They are not part of the infrastructure.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.11
- A GCP project with billing enabled. A dedicated project is recommended.
- [gcloud](https://cloud.google.com/sdk/docs/install) authenticated for Terraform:
  `gcloud auth application-default login`
- Your account needs, on the project: Owner, or Editor + Project IAM Admin + Secret Manager Admin
  + Service Usage Admin.

Terraform enables every API it needs. Images come from the public GitHub Container Registry
(`ghcr.io/pelidum/mpac_server`, `ghcr.io/pelidum/mpac_ui`), which Cloud Run pulls directly, so
you don't need a registry of your own.

## Quick start

```bash
cd terraform/gcloud
cp terraform.tfvars.example terraform.tfvars
#   set project_id, region, and the first admin's email/password

terraform init
terraform apply        # the first apply takes ~10 min, mostly Cloud SQL creation

terraform output ui_url
```

Open the UI URL and sign in as the onboarding admin.

## Cost

| Resource | Configuration | ~Monthly |
|---|---|---|
| Cloud SQL instance | db-f1-micro, ENTERPRISE edition, zonal | $7.50 |
| Cloud SQL storage + backups | 10 GB SSD, 7 daily backups | $2.20 |
| Cloud Run server | 2 vCPU / 2 GiB, scale-to-zero, billed while an instance is up | $2+ |
| Cloud Run UI | 1 vCPU / 512 MiB, scale-to-zero, billed per request | <$1 |
| Secret Manager, logging metrics, egress | | ~$1 |

Cost guardrails built in:

- **Scale to zero** on both services (`*_min_instances = 0`). Setting either to 1 removes cold
  starts but adds roughly $5–60/month depending on CPU.
- **Instance caps** (`*_max_instances = 3`). The server is billed for as long as an instance is
  up, so the cap bounds runaway spend. A precondition also refuses configs where
  `server_max_instances × server_db_pool_size` would exhaust `db_max_connections`.
- **ENTERPRISE edition** is pinned. PostgreSQL 16+ instances otherwise default to Enterprise
  Plus, which has no shared-core tiers and costs several times more.
- **No VPC.** Cloud SQL uses a public IP with no authorized networks, and connections are only
  possible through the Cloud SQL Auth Proxy (IAM + client certificates). That avoids a
  Serverless VPC connector or Private Service Access, which would cost more than everything
  else here combined.
- **Optional budget alert:** set `billing_account_id` (+ `monthly_budget_usd`, default $30).

## Configuration

Only `project_id` and `region` are required. See [`variables.tf`](variables.tf) for everything
else. Commonly used settings:

| Variable | Default | Purpose |
|---|---|---|
| `server_admin_onboarding_id` / `_password` | `""` | First admin user, created on first boot |
| `google_oauth_client_id` / `_secret` | `""` | "Sign in with Google". Redirect URI: `<ui_url>/callback/google` |
| `postgres_password`, `mpac_jwt_secret` | generated | Supply your own if you prefer |
| `public_access_mode` | `invoker_iam_disabled` | See [Domain Restricted Sharing](#domain-restricted-sharing) |
| `alert_email` | `""` | Alerts on auth-failure/error spikes and UI downtime |
| `deletion_protection` | `true` | Protects the database and services |
| `server_image` / `ui_image` | `ghcr.io/pelidum/…:latest` | Pin a digest for reproducible deploys |
| `project_name` | `mpac` | Resource name prefix. Change it to run several instances in one project |

### Secrets and state

`postgres_password` and `mpac_jwt_secret` are generated with `random_password`, so they are
stored in Terraform state. The OAuth client secret and admin password you supply are written to
Secret Manager as **write-only** values and are never kept in state. After changing either of
them, bump `secrets_version` to push the new value.

Treat state as sensitive either way. For anything long-lived, move it to the private,
versioned GCS bucket this config can create:

```bash
# terraform.tfvars: create_state_bucket = true
terraform apply
terraform output -raw state_backend_config > backend.tf
terraform init -migrate-state
```

See [`backend.tf.example`](backend.tf.example).

## Domain Restricted Sharing

Making a Cloud Run service public normally means granting `roles/run.invoker` to `allUsers`.
Many organizations enforce the **Domain Restricted Sharing** org policy
(`constraints/iam.allowedPolicyMemberDomains`), which rejects that grant with:

```
Error 403: ... One or more users named in the policy do not belong to a permitted customer
(or: Policy member domain restricted)
```

This config supports two modes:

**`public_access_mode = "invoker_iam_disabled"` (default).** This turns off Cloud Run's invoker
IAM check on both services, so no `allUsers` binding exists and DRS never applies. No org-level
permissions are needed. Google recommends this approach for projects under DRS. Your org can
still forbid it with the `constraints/run.managed.requireInvokerIam` managed constraint. If it
does, apply fails with a constraint violation on the service, and you need the second mode.

**`public_access_mode = "allusers"`.** This grants `allUsers` the invoker role, which is the
classic approach. Under DRS it only works if an org admin has added a **tag-based exception**
to the policy and the project carries that tag. Once it's set up, pass the tag value and this
config binds it to the project before granting `allUsers`:

```hcl
public_access_mode = "allusers"
drs_tag_value      = "tagValues/123456789012"
```

One-time org-admin setup for the exception, needing Organization Policy Administrator and
Tag Administrator:

1. Create a tag key and value at the org, e.g. key `allUsersIngress`, value `True`:
   ```bash
   gcloud resource-manager tags keys create allUsersIngress --parent=organizations/ORG_ID
   gcloud resource-manager tags values create True --parent=ORG_ID/allUsersIngress
   ```
2. Replace the org's `iam.allowedPolicyMemberDomains` policy with a conditional one: keep the
   existing allowed customer IDs as the default rule, and add a rule that allows all values
   when the resource has the tag:
   ```yaml
   name: organizations/ORG_ID/policies/iam.allowedPolicyMemberDomains
   spec:
     rules:
       - condition:
           expression: "resource.matchTag('ORG_ID/allUsersIngress', 'True')"
         allowAll: true
       - values:
           allowedValues: ["C0xxxxxxx"]   # your existing Workspace customer ID(s)
   ```
   `gcloud org-policies set-policy policy.yaml`
3. Look up the value's ID (`gcloud resource-manager tags values describe ORG_ID/allUsersIngress/True`)
   and set `drs_tag_value` to it.

Whoever runs `terraform apply` also needs `roles/resourcemanager.tagUser` on the tag value to
bind it. Tag bindings and org-policy changes can take a few minutes to propagate. If the
`allUsers` grant still fails right after the tag binding is created, re-run `terraform apply`.

## Operations

```bash
# URLs and names
terraform output

# Logs
gcloud run services logs read "$(terraform output -raw server_service_name)" --region REGION --limit 100
gcloud run services logs tail "$(terraform output -raw ui_service_name)" --region REGION
```

**Updating to a new image.** Cloud Run only creates a new revision when the service template
changes, so re-applying with the same `:latest` tag is a no-op. Either pin `server_image` /
`ui_image` to a new digest and apply (recommended), or force a pull of the current tag:

```bash
gcloud run services update "$(terraform output -raw server_service_name)" \
  --region REGION --image ghcr.io/pelidum/mpac_server:latest
gcloud run services update "$(terraform output -raw ui_service_name)" \
  --region REGION --image ghcr.io/pelidum/mpac_ui:latest
```

Terraform won't see a drift from this because the image string is unchanged.

**Scaling up the database.** Set `db_tier = "db-g1-small"` (about +$18/month), or a dedicated
`db-custom-*` tier for heavier use, and raise `db_max_connections` if you raise the server caps.

**Tearing down.**

```bash
# terraform.tfvars: deletion_protection = false
terraform apply
terraform destroy
```

Enabled APIs are left on. Cloud SQL instance names can't be reused for about a week, which is
why the instance name has a random suffix.

## Troubleshooting

- **Server starts but runs stall.** The server must keep `cpu_idle = false`: inference runs as
  background tasks after the request returns, and a CPU-throttled instance starves them. Runs
  interrupted by scale-down resume automatically on the next cold start.
- **`Enterprise Plus edition does not support tier db-f1-micro`.** Something removed the
  explicit `edition = "ENTERPRISE"`.
- **Precondition failed on `google_cloud_run_v2_service.server`.**
  `server_max_instances × server_db_pool_size` exceeds `db_max_connections - 10`. Lower one of
  the first two or raise the third.
- **Cold starts feel slow.** Startup CPU boost is on. If that's not enough, set
  `ui_min_instances = 1` (cheap, request-billed). A warm server costs noticeably more because
  of instance billing.

## Testing the configuration

Offline plan tests with mocked providers (no credentials, nothing is created):

```bash
terraform init -backend=false
terraform test
```
