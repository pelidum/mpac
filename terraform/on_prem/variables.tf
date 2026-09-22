variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "mpac"
}

# PostgreSQL variables
variable "postgres_version" {
  type    = string
  default = "16"
}

variable "postgres_user" {
  type    = string
  default = "postgres"
}

variable "postgres_password" {
  type      = string
  sensitive = true
}

variable "postgres_db" {
  type    = string
  default = "mpac"
}

variable "postgres_data_path" {
  type        = string
  default     = "/var/lib/mpac/postgres"
  description = "Persistent host path for PostgreSQL data. Must survive reboots — never use /tmp."

  validation {
    condition     = !startswith(var.postgres_data_path, "/tmp")
    error_message = "postgres_data_path must not be under /tmp — data would be lost on reboot."
  }
}

variable "postgres_backup_path" {
  type        = string
  default     = "/var/lib/mpac/backups"
  description = "Host path for pg_dump backups. The backup container writes daily dumps here."
}

variable "postgres_backup_retention_days" {
  type        = number
  default     = 30
  description = "Number of days to retain pg_dump backups before automatic cleanup."
}

variable "postgres_backup_schedule" {
  type        = string
  default     = "0 2 * * *"
  description = "Cron schedule for pg_dump backups (default: daily at 02:00)."
}

# Shared secrets
variable "mpac_jwt_secret" {
  type        = string
  sensitive   = true
  description = "HS256 signing secret shared between server and UI for JWT-based auth"
}

# MPAC Server variables
variable "server_image" {
  type    = string
  default = "ghcr.io/pelidum/mpac_server:latest"
}

variable "server_port" {
  type        = number
  default     = 50051
  description = "Internal gRPC port"
}

variable "server_host" {
  type    = string
  default = "0.0.0.0"
}

variable "server_db_pool_size" {
  type    = number
  default = 100
}

variable "server_admin_onboarding_id" {
  type        = string
  default     = ""
  description = "Admin email for initial bootstrap. The server skips onboarding when an admin already exists, so this is safe to leave set after first use."
}

variable "server_admin_onboarding_password" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Admin password for initial bootstrap. Ignored when an admin already exists."
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

# MPAC UI variables
variable "ui_image" {
  type    = string
  default = "ghcr.io/pelidum/mpac_ui:latest"
}

variable "ui_port" {
  type        = number
  default     = 8080
  description = "Internal UI port (Envoy expects 8080)"
}

variable "ui_debug" {
  type    = bool
  default = false
}

variable "ui_grpc_insecure" {
  type        = bool
  default     = true
  description = "Have the UI use a plaintext gRPC channel to the server. On-prem terminates TLS at Envoy and the UI reaches the server over the private Docker network, so this defaults to true. Set false only if the server is fronted by a TLS endpoint the UI can verify."
}

variable "ui_system_user" {
  type        = string
  default     = "system@mpac.internal"
  description = "Internal service account identity used by the stale-run reaper and other background tasks."
}

variable "ui_trusted_proxies" {
  type        = string
  default     = "envoy,proxy"
  description = "Comma-separated list of trusted proxy hostnames or IPs for X-Forwarded-For. On-prem defaults to the Envoy container aliases."
}

variable "google_oauth_client_id" {
  type    = string
  default = ""
}

variable "google_oauth_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}

# Envoy Proxy variables
variable "envoy_image" {
  type    = string
  default = "envoyproxy/envoy:v1.29-latest"
}

variable "envoy_http_port" {
  type        = number
  default     = 80
  description = "External port for HTTP (redirects to HTTPS)"
}

variable "envoy_https_port" {
  type        = number
  default     = 443
  description = "External port for HTTPS UI access"
}

variable "envoy_grpc_port" {
  type        = number
  default     = 50051
  description = "External port for gRPC API (TLS)"
}

variable "envoy_bind_ip" {
  type    = string
  default = "0.0.0.0"
}

variable "envoy_config_path" {
  type        = string
  default     = ""
  description = "Path to the envoy.yaml config file on host. Leave empty to use the envoy.yaml shipped alongside this config."
}

# TLS variables
#
# Leave tls_cert_path / tls_key_path empty (the default) to have Terraform
# generate a self-signed certificate automatically. Set both to bring your own.
variable "tls_cert_path" {
  type        = string
  default     = ""
  description = "Path to an existing TLS certificate (.crt/.pem) on host. Leave empty to auto-generate a self-signed cert."
}

variable "tls_key_path" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Path to an existing TLS private key (.key/.pem) on host. Leave empty to auto-generate a self-signed cert."
}

variable "tls_domain" {
  type        = string
  default     = "localhost"
  description = "Common name / DNS SAN for the auto-generated self-signed certificate."
}

variable "tls_validity_days" {
  type        = number
  default     = 825
  description = "Validity period (in days) for the auto-generated self-signed certificate."
}
