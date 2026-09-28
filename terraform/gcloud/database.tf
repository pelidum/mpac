# Cloud SQL (PostgreSQL)
#
# Cost notes:
# - db-f1-micro (shared core) is the cheapest tier and is plenty for MPAC.
# - PostgreSQL 16+ instances default to the ENTERPRISE_PLUS edition, which only
#   offers dedicated perf-optimized machines (no shared core) at several times
#   the price. `edition = "ENTERPRISE"` must be explicit, or creating a
#   db-f1-micro instance fails.
# - ZONAL availability: HA doubles the instance cost.

# Instance names are globally unique and cannot be reused for about a week after
# deletion, so add a random suffix.
resource "random_id" "db_suffix" {
  byte_length = 4
}

resource "google_sql_database_instance" "main" {
  name             = "${local.prefix}-db-${random_id.db_suffix.hex}"
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = var.deletion_protection

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_size_gb
    disk_autoresize   = true

    # GCP-level protection, separate from Terraform's deletion_protection
    # above. It also guards against deletes made from the console or gcloud.
    deletion_protection_enabled = var.deletion_protection

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = false
      start_time                     = "03:00"
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = var.db_backup_retention_count
      }
    }

    # Public IP without a VPC is deliberate: a Serverless VPC connector or
    # Private Service Access would add more to the bill than the rest of this
    # deployment combined. It is still locked down:
    # - no authorized_networks, so no direct TCP access from anywhere
    # - TRUSTED_CLIENT_CERTIFICATE_REQUIRED, so only clients presenting a
    #   Cloud SQL-issued certificate can connect (the Cloud SQL Auth Proxy
    #   behind Cloud Run's /cloudsql volume mount, plus connectors)
    # - the proxy authorizes through IAM (roles/cloudsql.client), and the
    #   database password is still required on top of that
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "TRUSTED_CLIENT_CERTIFICATE_REQUIRED"
    }

    database_flags {
      name  = "max_connections"
      value = var.db_max_connections
    }
  }

  lifecycle {
    # disk_autoresize grows the disk out-of-band.
    ignore_changes = [settings[0].disk_size]
  }

  depends_on = [google_project_service.services]
}

resource "google_sql_database" "main" {
  name     = local.db_name
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "main" {
  name     = var.postgres_user
  instance = google_sql_database_instance.main.name
  password = local.postgres_password
}
