# Project & location
variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into. A dedicated project is recommended."
}

variable "region" {
  type        = string
  description = "GCP region for Cloud Run and Cloud SQL. Tier-1 regions (e.g. us-central1, europe-west1) are cheapest."
}

variable "project_name" {
  type        = string
  default     = "mpac"
  description = "Name prefix for all resources. Change it to run more than one MPAC deployment in the same project."

  validation {
    # Service account IDs ("<name>-server") are capped at 30 characters.
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 3-22 characters of lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "labels" {
  type        = map(string)
  default     = {}
  description = "Extra labels applied to every resource that supports them."
}

variable "deletion_protection" {
  type        = bool
  default     = true
  description = "Protect the database and Cloud Run services from deletion. Set false and apply before `terraform destroy`."
}

# Public access
variable "public_access_mode" {
  type        = string
  default     = "invoker_iam_disabled"
  description = <<-EOT
    How the Cloud Run services are made reachable from the internet (MPAC authenticates requests itself):
      invoker_iam_disabled - turn off Cloud Run's invoker IAM check. No allUsers binding, so it works
                             under Domain Restricted Sharing. Recommended.
      allusers             - grant roles/run.invoker to allUsers. Use only if your org enforces
                             constraints/run.managed.requireInvokerIam. See drs_tag_value.
  EOT

  validation {
    condition     = contains(["invoker_iam_disabled", "allusers"], var.public_access_mode)
    error_message = "public_access_mode must be \"invoker_iam_disabled\" or \"allusers\"."
  }
}

variable "drs_tag_value" {
  type        = string
  default     = ""
  description = "allusers mode only: tag value (\"tagValues/<id>\") that your org's Domain Restricted Sharing policy exempts. It is bound to the project before allUsers is granted. Leave empty if not needed."

  validation {
    condition     = var.drs_tag_value == "" || can(regex("^tagValues/[0-9]+$", var.drs_tag_value))
    error_message = "drs_tag_value must look like \"tagValues/123456789\"."
  }
}

# Database
variable "db_tier" {
  type        = string
  default     = "db-f1-micro"
  description = "Cloud SQL machine tier (ENTERPRISE edition). db-f1-micro is the cheapest; db-g1-small is the next step up."
}

variable "db_disk_size_gb" {
  type        = number
  default     = 10
  description = "Initial SSD size in GB. Grows automatically."
}

variable "db_max_connections" {
  type        = number
  default     = 100
  description = "PostgreSQL max_connections. Must be at least server_max_instances * server_db_pool_size + 10."
}

variable "db_backup_retention_count" {
  type        = number
  default     = 7
  description = "Number of daily automated backups to keep."
}

variable "postgres_user" {
  type        = string
  default     = "postgres"
  description = "Database user the server connects as."
}

variable "postgres_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Database password. Leave empty to generate one."
}

# Shared secrets
variable "mpac_jwt_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "HS256 signing secret shared by the server and UI. Leave empty to generate one."
}

variable "secrets_version" {
  type        = number
  default     = 1
  description = "Bump this to push changed values of google_oauth_client_secret or server_admin_onboarding_password to Secret Manager (they are write-only and never kept in state)."
}

# Server
variable "server_image" {
  type        = string
  default     = "ghcr.io/pelidum/mpac_server:latest"
  description = "MPAC gRPC server image. Pin a digest (image@sha256:...) for reproducible deploys."
}

variable "server_cpu" {
  type        = string
  default     = "2"
  description = "vCPUs per server instance."
}

variable "server_memory" {
  type        = string
  default     = "2Gi"
  description = "Memory per server instance."
}

variable "server_min_instances" {
  type        = number
  default     = 0
  description = "Minimum server instances. 0 scales to zero when idle; runs orphaned by scale-down resume on the next cold start."
}

variable "server_max_instances" {
  type        = number
  default     = 3
  description = "Maximum server instances. The server is billed per instance while up, so this caps cost; it is also limited by db_max_connections."
}

variable "server_db_pool_size" {
  type        = number
  default     = 20
  description = "Database connection pool size per server instance."
}

variable "server_admin_onboarding_id" {
  type        = string
  default     = ""
  description = "Email of the first admin user, created on first boot. Ignored once an admin exists, so it is safe to leave set."
}

variable "server_admin_onboarding_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Password for the first admin user. Ignored once an admin exists. Stored write-only in Secret Manager."
}

variable "server_enable_reflection" {
  type        = bool
  default     = false
  description = "Expose the gRPC reflection API. Enable only for development or debugging."
}

variable "server_max_message_size_mb" {
  type        = number
  default     = 50
  description = "Maximum gRPC message size in megabytes (send and receive)."
}

# UI
variable "ui_image" {
  type        = string
  default     = "ghcr.io/pelidum/mpac_ui:latest"
  description = "MPAC UI image. Pin a digest (image@sha256:...) for reproducible deploys."
}

variable "ui_cpu" {
  type        = string
  default     = "1"
  description = "vCPUs per UI instance."
}

variable "ui_memory" {
  type        = string
  default     = "512Mi"
  description = "Memory per UI instance."
}

variable "ui_min_instances" {
  type        = number
  default     = 0
  description = "Minimum UI instances. 0 scales to zero when idle."
}

variable "ui_max_instances" {
  type        = number
  default     = 3
  description = "Maximum UI instances. The UI is billed per request, so this is only a ceiling on runaway cost."
}

variable "ui_debug" {
  type        = bool
  default     = false
  description = "Enable UI debug mode. Never in production."
}

variable "ui_system_user" {
  type        = string
  default     = "system@mpac.internal"
  description = "Internal identity used by the stale-run reaper and other background tasks."
}

variable "google_oauth_client_id" {
  type        = string
  default     = ""
  description = "Google OAuth client ID for \"Sign in with Google\". Leave empty for password login only."
}

variable "google_oauth_client_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Google OAuth client secret. Stored write-only in Secret Manager."
}

# Monitoring & cost
variable "alert_email" {
  type        = string
  default     = ""
  description = "Email for alerts (auth-failure and error spikes, UI uptime, budget). Leave empty to skip alerting."
}

variable "billing_account_id" {
  type        = string
  default     = ""
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) for a budget alert. Leave empty to skip."
}

variable "monthly_budget_usd" {
  type        = number
  default     = 30
  description = "Monthly budget in USD. Alerts at 50%, 90% and 100%. Used only when billing_account_id is set."
}

# State
variable "create_state_bucket" {
  type        = bool
  default     = false
  description = "Create a private, versioned GCS bucket for Terraform remote state. See backend.tf.example."
}
