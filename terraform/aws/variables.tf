# Location & naming
variable "region" {
  type        = string
  description = "AWS region, e.g. us-east-1. us-east-1 and us-east-2 are usually cheapest."
}

variable "project_name" {
  type        = string
  default     = "mpac"
  description = "Name prefix for all resources. Change it to run more than one MPAC deployment in the same account and region."

  validation {
    # Target group names ("<name>-grpc") are capped at 32 characters.
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 3-22 characters of lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Extra tags applied to every resource."
}

variable "deletion_protection" {
  type        = bool
  default     = true
  description = "Protect the database and load balancer from deletion. Set false and apply before `terraform destroy`."
}

# Network
variable "vpc_cidr" {
  type        = string
  default     = "10.20.0.0/16"
  description = "CIDR for the new VPC. Change it if it overlaps a network you peer with."
}

variable "allowed_ingress_cidrs" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "IPv4 CIDRs allowed to reach the load balancer (ports 80, 443, 50051). Restrict to your office/VPN ranges for a private deployment."
}

# TLS
variable "domain_name" {
  type        = string
  default     = ""
  description = "Hostname to serve MPAC on (e.g. mpac.example.com). With route53_zone_id, gets a trusted ACM certificate and DNS record automatically. Leave empty for a self-signed certificate on the ALB hostname."
}

variable "route53_zone_id" {
  type        = string
  default     = ""
  description = "Route 53 hosted zone for domain_name. Required when domain_name is set, unless acm_certificate_arn is."

  validation {
    condition     = var.domain_name == "" || var.route53_zone_id != "" || var.acm_certificate_arn != ""
    error_message = "domain_name needs route53_zone_id (automatic certificate + DNS) or acm_certificate_arn (your own certificate)."
  }
}

variable "acm_certificate_arn" {
  type        = string
  default     = ""
  description = "Existing ACM certificate (in this region) to use instead of generating one. Point your DNS at the alb_dns_name output."
}

variable "tls_validity_days" {
  type        = number
  default     = 825
  description = "Self-signed certificate lifetime in days (self-signed mode only)."
}

variable "tls_policy" {
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  description = "ALB TLS security policy. The default allows TLS 1.2 and 1.3 with modern ciphers."
}

# Compute
variable "task_cpu" {
  type        = number
  default     = 512
  description = "Fargate task CPU units, shared by server and UI (1024 = 1 vCPU). The server mostly waits on model APIs."

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.task_cpu)
    error_message = "task_cpu must be a Fargate CPU size: 256, 512, 1024, 2048, 4096, 8192 or 16384."
  }
}

variable "task_memory" {
  type        = number
  default     = 2048
  description = "Fargate task memory in MiB, shared by server and UI. Must be valid for task_cpu. PDF export needs headroom."
}

variable "use_spot" {
  type        = bool
  default     = false
  description = "Run on Fargate Spot (~70% cheaper compute). AWS may reclaim the task with 2 minutes' notice, so expect brief outages; interrupted runs resume automatically."
}

variable "enable_container_insights" {
  type        = bool
  default     = false
  description = "Enable ECS Container Insights (extra CloudWatch metrics, billed)."
}

variable "enable_ecs_exec" {
  type        = bool
  default     = false
  description = "Allow `aws ecs execute-command` shells into the containers. Debugging only."
}

variable "log_retention_days" {
  type        = number
  default     = 30
  description = "CloudWatch log retention in days."
}

# Database
variable "db_instance_class" {
  type        = string
  default     = "db.t4g.micro"
  description = "RDS instance class. db.t4g.micro is the cheapest; db.t4g.small is the next step up."
}

variable "db_allocated_storage_gb" {
  type        = number
  default     = 20
  description = "Initial gp3 storage in GB (20 is the gp3 minimum)."
}

variable "db_max_allocated_storage_gb" {
  type        = number
  default     = 100
  description = "Storage autoscaling ceiling in GB."
}

variable "db_max_connections" {
  type        = number
  default     = 100
  description = "PostgreSQL max_connections. Must be at least 2 * server_db_pool_size + 10."
}

variable "db_backup_retention_days" {
  type        = number
  default     = 7
  description = "Days of automated backups (point-in-time recovery window)."
}

variable "postgres_user" {
  type        = string
  default     = "postgres"
  description = "Database master user the server connects as."
}

variable "postgres_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Database password. Leave empty to generate one. Never stored in state."
}

# Shared secrets
variable "mpac_jwt_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "HS256 signing secret shared by the server and UI. Leave empty to generate one. Never stored in state."
}

variable "secrets_version" {
  type        = number
  default     = 1
  description = "Bump to rotate/push all secrets: generates new DB password and JWT secret (unless supplied), rewrites SSM, and redeploys the task. Rotating the JWT secret signs everyone out."
}

# Server
variable "server_image" {
  type        = string
  default     = "ghcr.io/pelidum/mpac_server:latest"
  description = "MPAC gRPC server image. Pin a digest (image@sha256:...) for reproducible deploys."
}

variable "server_db_pool_size" {
  type        = number
  default     = 20
  description = "Database connection pool size for the server."
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
  description = "Password for the first admin user. Ignored once an admin exists. Never stored in state."
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
  description = "Google OAuth client secret. Never stored in state."
}

# Monitoring & cost
variable "alert_email" {
  type        = string
  default     = ""
  description = "Email for alarms (auth-failure/error spikes, unhealthy targets, 5xx, low DB storage) and the budget. Leave empty to skip. Confirm the SNS subscription email after the first apply."
}

variable "monthly_budget_usd" {
  type        = number
  default     = 100
  description = "Account-wide monthly budget in USD (alerts at 50%, 90%, forecast 100%). Used only when alert_email is set."
}

# State
variable "create_state_bucket" {
  type        = bool
  default     = false
  description = "Create a private, versioned S3 bucket for Terraform remote state. See backend.tf.example."
}
