# Secrets
#
# Nothing secret is stored in Terraform state (the self-signed TLS key in
# tls.tf is the one exception):
# - The DB password and JWT secret come from ephemeral random_password (or
#   your own values). Ephemeral values are never persisted.
# - They are written only through write-only arguments (RDS password_wo, SSM
#   value_wo), which are sent to AWS and never recorded.
# Write-only values are only sent when their *_wo_version changes. So an
# ephemeral password regenerated on a later plan is simply ignored, and
# bumping var.secrets_version rotates every secret together (the DB password
# and its SSM copy stay in sync) and rolls the ECS service to pick them up.
#
# SSM Parameter Store SecureString rather than Secrets Manager: the standard
# tier is free (Secrets Manager is $0.40/secret/month), and Secrets Manager's
# managed rotation would break the server, which reads the DB password once at
# startup.

ephemeral "random_password" "postgres" {
  length  = 32
  special = false
}

ephemeral "random_password" "jwt" {
  length  = 64
  special = false
}

locals {
  postgres_password = var.postgres_password != "" ? var.postgres_password : ephemeral.random_password.postgres.result
  mpac_jwt_secret   = var.mpac_jwt_secret != "" ? var.mpac_jwt_secret : ephemeral.random_password.jwt.result

  oauth_enabled       = var.google_oauth_client_id != ""
  admin_password_set  = nonsensitive(var.server_admin_onboarding_password != "")
  create_admin_secret = var.server_admin_onboarding_id != "" && local.admin_password_set

  ssm_prefix = "/${local.prefix}"
}

resource "aws_ssm_parameter" "db_password" {
  name             = "${local.ssm_prefix}/db-password"
  description      = "MPAC PostgreSQL password"
  type             = "SecureString"
  value_wo         = local.postgres_password
  value_wo_version = var.secrets_version
}

resource "aws_ssm_parameter" "jwt_secret" {
  name             = "${local.ssm_prefix}/jwt-secret"
  description      = "MPAC HS256 JWT signing secret (server and UI)"
  type             = "SecureString"
  value_wo         = local.mpac_jwt_secret
  value_wo_version = var.secrets_version
}

resource "aws_ssm_parameter" "oauth_client_secret" {
  count            = local.oauth_enabled ? 1 : 0
  name             = "${local.ssm_prefix}/oauth-client-secret"
  description      = "MPAC Google OAuth client secret"
  type             = "SecureString"
  value_wo         = var.google_oauth_client_secret
  value_wo_version = var.secrets_version
}

resource "aws_ssm_parameter" "admin_password" {
  count            = local.create_admin_secret ? 1 : 0
  name             = "${local.ssm_prefix}/admin-onboarding-password"
  description      = "MPAC first-admin onboarding password"
  type             = "SecureString"
  value_wo         = var.server_admin_onboarding_password
  value_wo_version = var.secrets_version
}
