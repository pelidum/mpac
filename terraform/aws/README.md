# MPAC on AWS (Terraform)

Run MPAC on AWS with **one `terraform apply`**: an Application Load Balancer in front of a
Fargate task (gRPC server + web UI), and RDS for PostgreSQL. Only `region` is required.

Expect **about $65/month** with the defaults, or about $50 on Fargate Spot. Unlike the Google
Cloud deployment, nothing here scales to zero: AWS has no serverless front door that carries
gRPC, so the load balancer, one task and the database run all the time. See
[`../DEPLOYMENT.md`](../DEPLOYMENT.md) for a side-by-side comparison.

## Architecture

```
Internet ─► ALB (public subnets, TLS 1.2/1.3)       same ports as the on-prem Envoy proxy
             :80    → 301 redirect to :443
             :443   → UI      (HTTP → :8080, health /healthz)
             :50051 → server  (gRPC/h2c → :50051, health grpc.health.v1.Health/Check)
                │  task security group: only the ALB may connect
             ECS Fargate service: 1 task, 2 containers, public IP for egress (no NAT)
               ui ──localhost:50051, plaintext──► server
                │  database security group: only the task may connect; TLS required
             RDS PostgreSQL 16, db.t4g.micro, isolated subnets, not publicly accessible
```

- The ALB does the job Envoy does on-prem (TLS termination, HTTP→HTTPS redirect, UI and gRPC
  on separate ports), so clients connect the same way to either deployment.
- Server and UI share one task and talk over localhost, as they do over the private Docker
  network on-prem.
- Secrets live in **SSM Parameter Store** (SecureString) and are injected by ECS at startup.
  **No secret is ever written to Terraform state**; see [Secrets](#secrets).
- Inference backends are registered after deploying through the MPAC API/UI (`CreateBackend`).
  They are not part of the infrastructure.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.11
- AWS credentials for the target account (`AWS_PROFILE`, SSO or environment variables), with
  permission to manage VPC/EC2 networking, ELB, ACM, ECS, RDS, IAM roles, SSM, CloudWatch,
  SNS, Budgets, S3 and (for custom domains) Route 53. `AdministratorAccess` in a dedicated
  account is simplest.

Images come from the public GitHub Container Registry (`ghcr.io/pelidum/mpac_server`,
`ghcr.io/pelidum/mpac_ui`), which Fargate pulls directly, so you don't need ECR. They are
built for x86_64 only, so the task runs on x86 Fargate.

## Quick start

```bash
cd terraform/aws
cp terraform.tfvars.example terraform.tfvars
#   set region and the first admin's email/password

terraform init
terraform apply        # the first apply takes ~10-15 min, mostly RDS creation

terraform output ui_url
```

Open the UI URL, accept the self-signed certificate warning (or see [TLS](#tls) for a real
certificate), and sign in as the onboarding admin.

## Cost

us-east-1 on-demand prices, default settings:

| Resource | Configuration | ~Monthly |
|---|---|---|
| Application Load Balancer | hourly + light usage | $18 |
| Public IPv4 addresses | 2 for the ALB, 1 for the task | $11 |
| Fargate task | 0.5 vCPU / 2 GB, x86, always on | $21 |
| RDS | db.t4g.micro single-AZ + 20 GB gp3 | $14 |
| CloudWatch Logs, SSM, KMS | 30-day log retention | ~$1 |
| **Total** | | **~$65** |

Choices that keep it there:

- **No NAT gateway** (it would add about $33/month). The task gets a public IP for outbound
  calls (model APIs, image pulls), but its security group only accepts connections from the
  load balancer.
- **One task** for server and UI, so one network interface and one public IP.
- **No VPC endpoints**, Container Insights, ALB access logs or Secrets Manager (all billed).
- **`use_spot = true`** cuts compute by about 70% (to about $50 total). AWS can reclaim a Spot
  task with 2 minutes' notice, and the site is down until the replacement starts (typically
  1–2 minutes). Interrupted runs resume automatically.
- New AWS accounts' RDS free tier covers db.t4g.micro for 12 months (about −$14).
- **Budget alert:** set `alert_email` (with `monthly_budget_usd`, default $100). The budget
  covers the whole account.

To scale up, raise `task_cpu` / `task_memory` (valid Fargate combinations only) or
`db_instance_class`.

## TLS

The load balancer needs a certificate. Three modes, chosen by variables:

| Mode | Set | Result |
|---|---|---|
| **Self-signed** (default) | nothing | Certificate generated in Terraform for the ALB hostname (as on-prem) and imported into ACM. Browsers warn. |
| **Route 53** | `domain_name`, `route53_zone_id` | Publicly trusted ACM certificate, DNS-validated, plus an alias record. Fully automatic. HSTS enabled. |
| **Bring your own** | `acm_certificate_arn` (+ `domain_name` for the outputs) | Uses your ACM certificate. Point your DNS (CNAME) at the `alb_dns_name` output. |

**gRPC clients with the self-signed certificate** must trust it explicitly, as on-prem:

```bash
terraform output -raw tls_certificate_pem > mpac.crt
grpcurl -cacert mpac.crt "$(terraform output -raw grpc_endpoint)" grpc.health.v1.Health/Check
```

The self-signed certificate is tied to the ALB hostname, which never changes unless the load
balancer is replaced. Use a real domain for anything beyond evaluation, and for Google sign-in.

## Configuration

Only `region` is required. See [`variables.tf`](variables.tf) for everything else. Commonly
used settings:

| Variable | Default | Purpose |
|---|---|---|
| `server_admin_onboarding_id` / `_password` | `""` | First admin user, created on first boot |
| `domain_name`, `route53_zone_id`, `acm_certificate_arn` | `""` | See [TLS](#tls) |
| `allowed_ingress_cidrs` | `["0.0.0.0/0"]` | Restrict who can reach the ALB (e.g. office/VPN ranges) |
| `google_oauth_client_id` / `_secret` | `""` | "Sign in with Google". Redirect URI: `<ui_url>/callback/google` |
| `use_spot` | `false` | Fargate Spot (~70% cheaper compute, brief outages possible) |
| `task_cpu` / `task_memory` | `512` / `2048` | Task size, shared by server and UI |
| `alert_email` | `""` | Alarms (auth/error spikes, unhealthy targets, 5xx, low DB storage) and budget |
| `deletion_protection` | `true` | Protects the database and load balancer |
| `server_image` / `ui_image` | `ghcr.io/pelidum/…:latest` | Pin a digest for reproducible deploys |
| `project_name` | `mpac` | Resource name prefix. Change it to run several instances in one account/region |

## Secrets

The database password and JWT secret are generated on the first apply with
`ephemeral "random_password"`. The OAuth client secret and admin password are ones you supply.
All of them are written to SSM Parameter Store, and the database password to RDS, only
through Terraform's **write-only** arguments. They never appear in state or plan files.

Write-only values are only sent when `secrets_version` changes. To rotate, bump it:

```hcl
secrets_version = 2
```

`terraform apply` then generates a new database password and JWT secret (unless you supplied
your own), updates RDS and SSM together, and rolls the ECS service so the containers pick up
the new values. Rotating the JWT secret signs everyone out. Changing a supplied value (e.g.
`google_oauth_client_secret`) also needs a `secrets_version` bump to take effect.

The one exception: in self-signed mode the TLS private key is kept in state, because ACM
import needs it. Treat state as sensitive and keep it in the private bucket below.

### Remote state

```bash
# terraform.tfvars: create_state_bucket = true
terraform apply
terraform output -raw state_backend_config > backend.tf
terraform init -migrate-state
```

The bucket is versioned, encrypted, blocks all public access and denies non-TLS requests.
Locking uses S3's native lockfile, so no DynamoDB table is needed. See
[`backend.tf.example`](backend.tf.example).

## Operations

```bash
# URLs, names, endpoints
terraform output

# Logs (streams are prefixed server/ and ui/)
aws logs tail "$(terraform output -raw log_group_name)" --follow --log-stream-name-prefix server
```

**Updating to a new image.** ECS only starts new tasks when the task definition changes, so
re-applying with the same `:latest` tag is a no-op. Either pin `server_image` / `ui_image` to
a new digest and apply (recommended), or force a pull of the current tag:

```bash
aws ecs update-service --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" --force-new-deployment
```

Deployments start the new task before stopping the old one and roll back automatically if it
never becomes healthy.

**Shell into a container** (debugging only): set `enable_ecs_exec = true`, apply, then
`aws ecs execute-command --cluster … --task <task-id> --container server --interactive --command /bin/sh`.

**Tearing down.**

```bash
# terraform.tfvars: deletion_protection = false
terraform apply
terraform destroy
```

RDS takes a final snapshot on destroy (`<project_name>-db-final-<suffix>`), which keeps costing
storage until you delete it.

## Troubleshooting

- **Task keeps restarting / deployment rolls back.** Check the `server/` log stream. A
  database auth failure after a manual SSM change means RDS and SSM disagree; bump
  `secrets_version` to resync both.
- **UI loads but gRPC clients fail with certificate errors.** In self-signed mode, pass the
  exported `mpac.crt` as the client's CA.
- **`2 * server_db_pool_size must be <= db_max_connections - 10`.** Two tasks overlap during
  deploys, each with its own pool. Lower `server_db_pool_size` or raise `db_max_connections`.
- **Runs stop when AWS reclaims a Spot task.** They resume automatically on the next start. Set
  `use_spot = false` if brief outages are unacceptable.
- **No alarm emails.** Confirm the SNS subscription email AWS sends after the first apply.

## Testing the configuration

Offline tests with a mocked AWS provider (no credentials, nothing is created):

```bash
terraform init -backend=false
terraform test
```
