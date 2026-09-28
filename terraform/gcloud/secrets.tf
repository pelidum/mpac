# Secrets
#
# The database password and JWT secret are generated unless you supply your
# own, so there is nothing to invent by hand. Generated values necessarily live
# in Terraform state, so treat state as sensitive (see state.tf).
#
# Every secret is stored in Secret Manager and injected into Cloud Run with
# secret_key_ref, so none of them appear in Cloud Run revision metadata.
# Secrets you supply (OAuth client secret, admin onboarding password) are
# written with the write-only secret_data_wo, so they are never stored in state.
# After changing one of them, bump var.secrets_version to push the new value.

resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

locals {
  postgres_password = var.postgres_password != "" ? var.postgres_password : random_password.postgres.result
  mpac_jwt_secret   = var.mpac_jwt_secret != "" ? var.mpac_jwt_secret : random_password.jwt.result

  oauth_enabled       = var.google_oauth_client_id != ""
  admin_password_set  = nonsensitive(var.server_admin_onboarding_password != "")
  create_admin_secret = var.server_admin_onboarding_id != "" && local.admin_password_set
}

# Database password (server only)
resource "google_secret_manager_secret" "db_password" {
  secret_id = "${local.prefix}-db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = local.postgres_password
}

# JWT signing secret (server and UI)
resource "google_secret_manager_secret" "jwt_secret" {
  secret_id = "${local.prefix}-jwt-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "jwt_secret" {
  secret      = google_secret_manager_secret.jwt_secret.id
  secret_data = local.mpac_jwt_secret
}

# Google OAuth client secret (UI only, optional)
resource "google_secret_manager_secret" "oauth_client_secret" {
  count     = local.oauth_enabled ? 1 : 0
  secret_id = "${local.prefix}-oauth-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "oauth_client_secret" {
  count                  = local.oauth_enabled ? 1 : 0
  secret                 = google_secret_manager_secret.oauth_client_secret[0].id
  secret_data_wo         = var.google_oauth_client_secret
  secret_data_wo_version = var.secrets_version
}

# Admin onboarding password (server only, optional)
resource "google_secret_manager_secret" "admin_password" {
  count     = local.create_admin_secret ? 1 : 0
  secret_id = "${local.prefix}-admin-onboarding-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "admin_password" {
  count                  = local.create_admin_secret ? 1 : 0
  secret                 = google_secret_manager_secret.admin_password[0].id
  secret_data_wo         = var.server_admin_onboarding_password
  secret_data_wo_version = var.secrets_version
}
