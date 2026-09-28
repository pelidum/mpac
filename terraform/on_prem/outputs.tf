locals {
  # Omit the port from URLs when Envoy serves HTTPS on the default port.
  ui_origin = var.envoy_https_port == 443 ? "https://${var.tls_domain}" : "https://${var.tls_domain}:${var.envoy_https_port}"
}

output "ui_url" {
  value       = local.ui_origin
  description = "MPAC web UI (hostname from tls_domain)."
}

output "grpc_endpoint" {
  value       = "${var.tls_domain}:${var.envoy_grpc_port}"
  description = "MPAC gRPC server (TLS). Clients must trust generated/envoy.crt unless you brought your own certificate."
}

output "google_oauth_redirect_uri" {
  value       = "${local.ui_origin}/callback/google"
  description = "Authorized redirect URI to add to your Google OAuth client (see ../DEPLOYMENT.md#google-sign-in-optional)."
}
