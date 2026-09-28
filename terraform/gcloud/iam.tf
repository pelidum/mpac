# Service accounts
#
# One identity per service, least privilege. No keys are created: Cloud Run
# gets credentials from its attached service account.

resource "google_service_account" "server" {
  account_id   = "${local.prefix}-server"
  display_name = "MPAC gRPC server (Cloud Run)"
  depends_on   = [google_project_service.services]
}

resource "google_service_account" "ui" {
  account_id   = "${local.prefix}-ui"
  display_name = "MPAC UI (Cloud Run)"
  depends_on   = [google_project_service.services]
}

# The Cloud SQL Auth Proxy needs cloudsql.instances.connect, which Cloud SQL
# only grants through project-level IAM (there are no per-instance IAM
# policies). Only the server identity gets it.
resource "google_project_iam_member" "server_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = google_service_account.server.member
}

# Per-secret access instead of project-wide secretAccessor.
locals {
  secret_access = merge(
    {
      "server-db-password" = { secret = google_secret_manager_secret.db_password.id, member = google_service_account.server.member }
      "server-jwt"         = { secret = google_secret_manager_secret.jwt_secret.id, member = google_service_account.server.member }
      "ui-jwt"             = { secret = google_secret_manager_secret.jwt_secret.id, member = google_service_account.ui.member }
    },
    local.oauth_enabled ? {
      "ui-oauth" = { secret = google_secret_manager_secret.oauth_client_secret[0].id, member = google_service_account.ui.member }
    } : {},
    local.create_admin_secret ? {
      "server-admin-password" = { secret = google_secret_manager_secret.admin_password[0].id, member = google_service_account.server.member }
    } : {},
  )
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_access
  secret_id = each.value.secret
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value.member
}
