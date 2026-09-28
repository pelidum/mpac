output "ui_url" {
  value       = google_cloud_run_v2_service.ui.uri
  description = "MPAC web UI."
}

output "server_url" {
  value       = google_cloud_run_v2_service.server.uri
  description = "MPAC gRPC server (use host:443 with TLS)."
}

output "server_service_name" {
  value       = google_cloud_run_v2_service.server.name
  description = "Cloud Run service name for the server (for gcloud logs / redeploys)."
}

output "ui_service_name" {
  value       = google_cloud_run_v2_service.ui.name
  description = "Cloud Run service name for the UI (for gcloud logs / redeploys)."
}

output "db_connection_name" {
  value       = google_sql_database_instance.main.connection_name
  description = "Cloud SQL connection name (for the Cloud SQL Auth Proxy)."
}

output "server_service_account" {
  value       = google_service_account.server.email
  description = "Server service account."
}

output "ui_service_account" {
  value       = google_service_account.ui.email
  description = "UI service account."
}

output "state_backend_config" {
  value = var.create_state_bucket ? join("\n", [
    "terraform {",
    "  backend \"gcs\" {",
    "    bucket = \"${google_storage_bucket.tf_state[0].name}\"",
    "    prefix = \"${local.prefix}\"",
    "  }",
    "}",
  ]) : null
  description = "Paste into backend.tf, then run `terraform init -migrate-state`."
}

output "google_oauth_redirect_uri" {
  value       = "${google_cloud_run_v2_service.ui.uri}/callback/google"
  description = "Authorized redirect URI to add to your Google OAuth client (see ../DEPLOYMENT.md#google-sign-in-optional)."
}
