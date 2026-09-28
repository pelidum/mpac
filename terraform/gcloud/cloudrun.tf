# Cloud Run services
#
# Both services scale to zero when idle. Both are reachable from the internet
# and do their own authentication (JWT sessions and API keys). The server
# cannot be made private (ingress INTERNAL_ONLY or IAM-only invocation):
# - the UI calls it at its public *.run.app URL, and Cloud Run egress without
#   a VPC is not "internal" traffic
# - the UI sends MPAC JWTs, not Google-signed ID tokens, so an IAM invoker
#   check would reject it

locals {
  invoker_iam_disabled = var.public_access_mode == "invoker_iam_disabled"
  bind_all_users       = var.public_access_mode == "allusers"
}

# gRPC server
resource "google_cloud_run_v2_service" "server" {
  name     = "${local.prefix}-server"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection  = var.deletion_protection
  invoker_iam_disabled = local.invoker_iam_disabled

  template {
    service_account = google_service_account.server.email

    # Benchmarks and long inference runs outlive the 300s default. 3600s is
    # the Cloud Run maximum. (The v2 API rejects the older
    # run.googleapis.com/timeout annotation, so use this field.)
    timeout = "3600s"

    scaling {
      min_instance_count = var.server_min_instances
      max_instance_count = var.server_max_instances
    }

    containers {
      image = var.server_image

      args = [
        "--db_host=/cloudsql/${google_sql_database_instance.main.connection_name}",
        "--db_user=${var.postgres_user}",
        "--db_pool_size=${var.server_db_pool_size}",
        "--host=0.0.0.0",
        "--port=50051",
      ]

      env {
        name = "MPAC_DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "MPAC_JWT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.jwt_secret.secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = var.server_admin_onboarding_id != "" ? [1] : []
        content {
          name  = "MPAC_ADMIN_ONBOARDING_ID"
          value = var.server_admin_onboarding_id
        }
      }

      dynamic "env" {
        for_each = local.create_admin_secret ? [1] : []
        content {
          name = "MPAC_ADMIN_ONBOARDING_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.admin_password[0].secret_id
              version = "latest"
            }
          }
        }
      }

      env {
        name  = "MPAC_MAX_MESSAGE_SIZE"
        value = tostring(var.server_max_message_size_mb * 1024 * 1024)
      }

      dynamic "env" {
        for_each = var.server_enable_reflection ? [1] : []
        content {
          name  = "MPAC_ENABLE_REFLECTION"
          value = "true"
        }
      }

      resources {
        limits = {
          cpu    = var.server_cpu
          memory = var.server_memory
        }
        # Inference runs as a background asyncio task AFTER the CreateTestRun
        # request completes. With cpu_idle = true (request-based billing),
        # Cloud Run throttles CPU to near zero between requests and starves
        # the event loop: connections time out, the OpenAI SDK retry-storms,
        # and runs stall. So CPU must stay allocated while an instance is up.
        # This switches the server to instance-based billing but does NOT
        # affect scale-to-zero. With min_instances = 0 it still scales down
        # when idle, and runs orphaned by scale-down are resumed on the next
        # cold start (startup resume + periodic sweeper).
        cpu_idle          = false
        startup_cpu_boost = true
      }

      ports {
        container_port = 50051
        name           = "h2c"
      }

      startup_probe {
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 10
        failure_threshold     = 5
        tcp_socket {
          port = 50051
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    precondition {
      # Each server instance opens up to server_db_pool_size connections.
      # Leave headroom for Cloud SQL's reserved superuser slots and ad-hoc
      # admin sessions.
      condition     = var.server_max_instances * var.server_db_pool_size <= var.db_max_connections - 10
      error_message = "server_max_instances * server_db_pool_size must be <= db_max_connections - 10, or scaled-out servers will exhaust database connections. Lower one of the first two or raise db_max_connections (and possibly db_tier)."
    }
  }

  depends_on = [
    google_sql_database.main,
    google_sql_user.main,
    google_project_iam_member.server_sql_client,
    google_secret_manager_secret_iam_member.access,
    google_secret_manager_secret_version.db_password,
    google_secret_manager_secret_version.jwt_secret,
    google_secret_manager_secret_version.admin_password,
  ]
}

# FastAPI UI
resource "google_cloud_run_v2_service" "ui" {
  name     = "${local.prefix}-ui"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection  = var.deletion_protection
  invoker_iam_disabled = local.invoker_iam_disabled

  template {
    service_account = google_service_account.ui.email
    timeout         = "3600s"

    scaling {
      min_instance_count = var.ui_min_instances
      max_instance_count = var.ui_max_instances
    }

    containers {
      image = var.ui_image

      env {
        name = "MPAC_JWT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.jwt_secret.secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = local.oauth_enabled ? [1] : []
        content {
          name  = "GOOGLE_OAUTH_CLIENT_ID"
          value = var.google_oauth_client_id
        }
      }

      dynamic "env" {
        for_each = local.oauth_enabled ? [1] : []
        content {
          name = "GOOGLE_OAUTH_CLIENT_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.oauth_client_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }

      # The UI reaches the server over its public HTTPS URL with a TLS gRPC
      # channel (MPAC_GRPC_INSECURE unset).
      env {
        name  = "MPAC_HOST"
        value = trimprefix(google_cloud_run_v2_service.server.uri, "https://")
      }

      env {
        name  = "MPAC_PORT"
        value = "443"
      }

      env {
        name  = "MPAC_SYSTEM_USER"
        value = var.ui_system_user
      }

      dynamic "env" {
        for_each = var.ui_debug ? [1] : []
        content {
          name  = "MPAC_DEBUG"
          value = "true"
        }
      }

      # Cloud Run's front end terminates TLS and always sets X-Forwarded-For,
      # and the container is not reachable any other way, so trust all sources.
      env {
        name  = "MPAC_TRUSTED_PROXIES"
        value = "*"
      }

      resources {
        limits = {
          cpu    = var.ui_cpu
          memory = var.ui_memory
        }
        # Request-based billing: the UI does no background work.
        cpu_idle          = true
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
        name           = "http1"
      }

      startup_probe {
        initial_delay_seconds = 5
        timeout_seconds       = 3
        period_seconds        = 10
        failure_threshold     = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_secret_manager_secret_iam_member.access,
    google_secret_manager_secret_version.jwt_secret,
    google_secret_manager_secret_version.oauth_client_secret,
  ]
}

# Public access, "allusers" mode
#
# Only needed when public_access_mode = "allusers". The default mode
# ("invoker_iam_disabled") switches off Cloud Run's invoker IAM check, so no
# allUsers binding exists and Domain Restricted Sharing never comes into play.
#
# Orgs that enforce constraints/run.managed.requireInvokerIam must use
# "allusers". If the org also enforces Domain Restricted Sharing
# (iam.allowedPolicyMemberDomains), those bindings are rejected with
# "403 ... Policy member domain restricted" unless the org policy has a
# tag-based exception. Set var.drs_tag_value to that tag value and it is bound
# to the project before the bindings are created. See README.md.

resource "google_tags_tag_binding" "drs_exception" {
  count     = local.bind_all_users && var.drs_tag_value != "" ? 1 : 0
  parent    = "//cloudresourcemanager.googleapis.com/projects/${data.google_project.main.number}"
  tag_value = var.drs_tag_value
}

# Replacing a service drops its IAM policy, but the binding's attributes (the
# name) don't change, so Terraform wouldn't notice. replace_triggered_by
# recreates the binding whenever its service is replaced.
resource "google_cloud_run_v2_service_iam_member" "server_public" {
  count = local.bind_all_users ? 1 : 0

  project  = google_cloud_run_v2_service.server.project
  location = google_cloud_run_v2_service.server.location
  name     = google_cloud_run_v2_service.server.name
  role     = "roles/run.invoker"
  member   = "allUsers"

  depends_on = [google_tags_tag_binding.drs_exception]

  lifecycle {
    replace_triggered_by = [google_cloud_run_v2_service.server.id]
  }
}

resource "google_cloud_run_v2_service_iam_member" "ui_public" {
  count = local.bind_all_users ? 1 : 0

  project  = google_cloud_run_v2_service.ui.project
  location = google_cloud_run_v2_service.ui.location
  name     = google_cloud_run_v2_service.ui.name
  role     = "roles/run.invoker"
  member   = "allUsers"

  depends_on = [google_tags_tag_binding.drs_exception]

  lifecycle {
    replace_triggered_by = [google_cloud_run_v2_service.ui.id]
  }
}
