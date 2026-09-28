# Deploying MPAC

MPAC ships three self-contained Terraform configurations. Each one is a single
`terraform apply` after filling in a `terraform.tfvars`, and each deploys the same two public
images (`ghcr.io/pelidum/mpac_server`, `ghcr.io/pelidum/mpac_ui`) with a PostgreSQL 16
database.

## At a glance

| | [On-prem](on_prem/) | [Google Cloud](gcloud/) | [AWS](aws/) |
|---|---|---|---|
| **Est. monthly cost** | Your hardware only | **~$13–15** | **~$65** (Spot: ~$50) |
| **Main cost drivers** | — | Cloud SQL instance (~$7.50) | Load balancer + public IPv4 (~$29), always-on task (~$21), RDS (~$14) |
| **Scale to zero** | n/a | Yes, both services | No, 1 task always on |
| **Cold starts** | None | A few seconds after idle | None |
| **Compute** | Docker on one host | Cloud Run: server 2 vCPU / 2 GiB, UI 1 vCPU / 512 MiB, up to 3 instances each | Fargate: one 0.5 vCPU / 2 GB x86 task running both containers |
| **Database** | Postgres container, data on a host path | Cloud SQL db-f1-micro, zonal | RDS db.t4g.micro, single-AZ |
| **Backups** | Daily `pg_dump`, 30 days, on the host | Daily, 7 kept (no point-in-time recovery) | Daily + point-in-time recovery, 7 days; final snapshot on destroy |
| **High availability** | None | Managed restarts; zonal DB | Managed restarts; single-AZ DB |
| **TLS certificate** | Self-signed (generated) or bring your own | Google-managed on `*.run.app`, trusted, nothing to do | Self-signed on the ALB hostname (default), ACM + Route 53 (automatic), or your own ACM cert |
| **Custom domain** | Bring your own cert | Not wired up (use Cloud Run domain mapping manually) | Yes (`domain_name`) |
| **Endpoints** | `https://<host>` (UI), `<host>:50051` (gRPC) | Two `https://…run.app` URLs; gRPC on `:443` | `https://<host>` (UI), `<host>:50051` (gRPC), same as on-prem |
| **gRPC client trust** | Must trust the self-signed cert | Nothing to do | Self-signed mode: must trust the exported cert |
| **Exposed to the internet** | Envoy ports 80/443/50051 on the host | Both Cloud Run services (app-level auth) | ALB ports 80/443/50051 only; can be restricted with `allowed_ingress_cidrs` |
| **Database exposure** | Docker network only | Public IP, no authorized networks, Cloud SQL client certs required (Auth Proxy only) | Private subnets, no internet route, task security group only, TLS required |
| **Google sign-in** | Optional; manual OAuth client ([below](#google-sign-in-optional)). Users click through the self-signed warning first | Optional; manual OAuth client. Works on the `*.run.app` URL | Optional; manual OAuth client. Works on the ALB hostname (after the self-signed warning) or your domain |
| **Secrets store** | `terraform.tfvars` → container env | Secret Manager | SSM Parameter Store (SecureString) |
| **Secrets in Terraform state** | Yes | Generated ones yes; ones you supply no | None (except the self-signed TLS key) |
| **Required tfvars** | `postgres_password`, `mpac_jwt_secret` | `project_id`, `region` | `region` |
| **Prerequisites** | Docker, ports 80/443/50051 free | GCP project with billing; `gcloud auth application-default login` | AWS account credentials with broad permissions |
| **Org-policy gotchas** | — | Domain Restricted Sharing blocks public Cloud Run by default; handled (see [gcloud README](gcloud/README.md#domain-restricted-sharing)) | SCPs may block resource types; none needed by default |
| **First apply** | ~1–2 min | ~10 min (Cloud SQL) | ~10–15 min (RDS) |
| **Deletion protection** | — | DB + Cloud Run services (`deletion_protection`) | DB + ALB (`deletion_protection`) |
| **Logs** | `docker logs` | Cloud Logging | CloudWatch Logs (30 days) |
| **Alerting** | — | Auth/error spikes, UI uptime, optional budget (`alert_email`) | Auth/error spikes, unhealthy targets, 5xx, DB storage, budget (`alert_email`) |
| **Remote state** | Local only | Optional GCS bucket (`create_state_bucket`) | Optional S3 bucket with native locking (`create_state_bucket`) |
| **Offline tests** | `terraform validate` | `terraform test` (mocked) | `terraform test` (mocked) |

Costs are list prices for light use (us-central1 / us-east-1), excluding model inference, which
is billed by your model providers. The cost section of each README has the breakdown.

## Choosing

- **On-prem:** one machine you already run, air-gapped or regulated networks, local models
  (e.g. Ollama on the same host), or zero cloud spend.
- **Google Cloud:** the cheapest managed option, especially for bursty or occasional use:
  scale-to-zero means idle time costs almost nothing. Trusted HTTPS with no domain needed.
- **AWS:** your organization standardizes on AWS, you want custom-domain HTTPS out of the box,
  or you want no cold starts. It costs more because nothing scales to zero.

## Deploying (all targets)

1. Clone the repository and pick a target directory:
   ```bash
   cd terraform/<on_prem|gcloud|aws>
   cp terraform.tfvars.example terraform.tfvars
   ```
2. Fill in the required values from the table above, plus the first admin user:
   ```hcl
   server_admin_onboarding_id       = "admin@example.com"
   server_admin_onboarding_password = "a-strong-password"
   ```
   The server creates this admin on first boot and ignores these values once an admin exists,
   so they're safe to leave set.
3. Deploy:
   ```bash
   terraform init
   terraform apply
   terraform output        # URLs and endpoints
   ```
4. Sign in to the UI as the admin, invite users, and register inference backends (Settings,
   or the `CreateBackend` RPC). Backends live in the database, not in Terraform.
5. For anything long-lived:
   - **Pin image digests** (`server_image = "ghcr.io/pelidum/mpac_server@sha256:…"`) so
     deploys are reproducible and upgrades are deliberate.
   - **Move state to a remote backend** (cloud targets: `create_state_bucket = true`, then
     follow `backend.tf.example`).
   - Set **`alert_email`** (cloud targets) for alarms and a budget.
   - Optionally enable [Google sign-in](#google-sign-in-optional).

Each target's README covers configuration in depth: [on_prem](on_prem/README.md),
[gcloud](gcloud/README.md), [aws](aws/README.md).

## Google sign-in (optional)

Password login always works. To add "Login with Google", create a Google OAuth client and give
MPAC its ID and secret. The button only appears on the login page once both are set.

Terraform can't create the client for you: Google has no public API for creating ordinary web
OAuth clients, so this is a one-time manual step in the Google Cloud Console. It takes about 5
minutes, is free, and works for every target, including on-prem and AWS. The client can live in
any Google Cloud project; it doesn't need billing or to be where MPAC runs.

1. **Deploy once without OAuth and get your redirect URI.** The URI depends on your MPAC
   hostname, so read it from the deployment:
   ```bash
   terraform output -raw google_oauth_redirect_uri
   # e.g. https://mpac-ui-123456789012.us-central1.run.app/callback/google
   ```
   It's always `<ui_url>/callback/google`. If you use your own domain, you know it in advance.
2. **Set up the consent screen.** In the [Google Cloud Console](https://console.cloud.google.com/),
   open **Google Auth Platform** (formerly "OAuth consent screen") and click **Get started**:
   - **Branding:** an app name (e.g. "MPAC") and a support email. Skip the logo: adding one
     triggers Google's brand verification review.
   - **Audience:**
     - **Internal** if your users are all in your Google Workspace organization. Only your
       domain can sign in, and there's no review. Recommended when available.
     - **External** otherwise. Then click **Publish app** to move it from *Testing* to
       *In production*. In Testing, only the test users you list can sign in; everyone else
       gets an "access blocked" error. MPAC only requests `openid`, `email` and `profile`, which
       don't need Google's verification review.
3. **Create the client.** Go to **Clients → Create client** and choose application type
   **Web application**. Under **Authorized redirect URIs**, add the URI from step 1 exactly
   (scheme, host and path must all match). You don't need JavaScript origins. Copy the
   **client ID** and **client secret**.
4. **Give MPAC the credentials** in `terraform.tfvars`, then `terraform apply`:
   ```hcl
   google_oauth_client_id     = "1234567890-abc.apps.googleusercontent.com"
   google_oauth_client_secret = "GOCSPX-..."
   ```
   On Google Cloud and AWS the secret is stored write-only. If you change it later, also bump
   `secrets_version` so the new value is pushed.
5. **Invite users in MPAC** (Admin page) by their Google email address. Signing in with Google
   doesn't create accounts: people who haven't been invited see "Access Denied: User not found".

**One client, many deployments.** Add each deployment's redirect URI to the same client, e.g.
staging and production.

**Troubleshooting:**

| Error | Cause |
|---|---|
| `redirect_uri_mismatch` | The URI on the client doesn't exactly match the host users signed in on. On Google Cloud, use the `ui_url` output; Cloud Run services also answer on a second URL that isn't registered. Behind your own domain, register the domain's URI. |
| "Access blocked" / "has not completed the Google verification process" | External app still in *Testing*. Publish it, or add the user as a test user. |
| `invalid_client` | Wrong client ID or secret, or the client was deleted. |
| "Access Denied: User not found" | Signed in fine, but the email isn't an MPAC user yet. Invite it. |

Google only checks that the redirect URI uses HTTPS on a real domain name (`localhost` also
works). It doesn't check the certificate, so sign-in works on self-signed deployments once the
browser warning has been accepted.

## Operations cheat sheet

| Task | On-prem | Google Cloud | AWS |
|---|---|---|---|
| Logs | `docker logs -f mpac-server` | `gcloud run services logs tail <service> --region <region>` | `aws logs tail /ecs/mpac --follow` |
| Deploy a new image | Change `server_image`/`ui_image` to a new tag or digest and apply | Change `server_image`/`ui_image` to a new digest and apply, or `gcloud run services update <service> --image …` | Change the image to a new digest and apply, or `aws ecs update-service … --force-new-deployment` |
| Rotate secrets | Change the values in tfvars and apply | Supplied: change the value and bump `secrets_version`. Generated: `terraform apply -replace=random_password.jwt` (or `.postgres`); instances pick up new values as they restart | Bump `secrets_version` (rotates the DB password and JWT secret and redeploys) |
| Scale up | Bigger host | `server_cpu`/`server_memory`, `*_max_instances`, `db_tier` | `task_cpu`/`task_memory`, `db_instance_class` |
| Database shell | `docker exec -it mpac-postgres psql -U postgres -d mpac` | Cloud SQL Studio, or `gcloud beta sql connect` (goes through the Auth Proxy) | Enable ECS Exec and connect from the server container, or use a bastion |
| Tear down | `terraform destroy` | Set `deletion_protection = false`, apply, then `terraform destroy` | Set `deletion_protection = false`, apply, then `terraform destroy` (a final DB snapshot is kept) |

On both clouds, re-applying with an unchanged `:latest` tag does **not** pull a newer image,
because the service definition hasn't changed. Pin digests or force a redeploy as shown.

Rotating the JWT secret signs every user out.

## Security notes

- **Authentication is in the application.** Every public endpoint (UI and gRPC) requires a
  login session (password or Google OAuth → JWT) or an API key. The infrastructure doesn't add
  a second auth layer. To limit who can even reach MPAC, restrict the network: firewall rules
  on-prem, `allowed_ingress_cidrs` on AWS, or Cloud Armor / IAP (not included) on Google Cloud.
- **Terraform state is sensitive.** On-prem state holds every secret. Google Cloud state holds
  the generated DB password and JWT secret. AWS state holds none, except the self-signed TLS
  key. Keep state in the provided private, versioned buckets and limit who can read them.
- **`terraform.tfvars` is git-ignored** in every target. Never commit it; prefer `TF_VAR_*`
  environment variables in CI.
- **Self-signed certificates** (on-prem, AWS default) encrypt traffic but can't prove identity
  to clients that don't already trust them. Distribute the certificate to gRPC clients
  (`generated/envoy.crt` on-prem, `terraform output -raw tls_certificate_pem` on AWS). Use a
  real certificate for production.
- **Database access** is never open to the internet in any target. See "Database exposure"
  above for how each one enforces it.
- **Deletion protection** is on by default in the cloud targets. Turning it off takes a
  deliberate apply before `destroy` works.
