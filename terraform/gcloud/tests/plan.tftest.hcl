# Offline plan tests. Providers are mocked, so no GCP credentials are needed
# and nothing is created. Run with: terraform init -backend=false && terraform test

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
}

mock_provider "random" {}

variables {
  project_id = "test-project"
  region     = "us-central1"
}

run "defaults" {
  command = plan

  assert {
    condition     = google_sql_database_instance.main.settings[0].edition == "ENTERPRISE"
    error_message = "Cloud SQL must pin ENTERPRISE edition (PG16+ defaults to ENTERPRISE_PLUS, which has no db-f1-micro)."
  }

  assert {
    condition     = google_sql_database_instance.main.settings[0].tier == "db-f1-micro"
    error_message = "Default tier should be the cheapest shared-core tier."
  }

  assert {
    condition     = google_sql_database_instance.main.settings[0].ip_configuration[0].ssl_mode == "TRUSTED_CLIENT_CERTIFICATE_REQUIRED"
    error_message = "Direct DB connections must require Cloud SQL client certificates."
  }

  assert {
    condition     = length(google_sql_database_instance.main.settings[0].ip_configuration[0].authorized_networks) == 0
    error_message = "No authorized networks may be configured on the public IP."
  }

  assert {
    condition     = google_cloud_run_v2_service.server.invoker_iam_disabled && google_cloud_run_v2_service.ui.invoker_iam_disabled
    error_message = "Default mode should disable the invoker IAM check on both services."
  }

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.server_public) == 0 && length(google_cloud_run_v2_service_iam_member.ui_public) == 0
    error_message = "Default mode must not create allUsers bindings (they trip Domain Restricted Sharing)."
  }

  assert {
    condition     = google_cloud_run_v2_service.server.template[0].containers[0].resources[0].cpu_idle == false
    error_message = "Server needs cpu_idle = false so background inference is not CPU-starved."
  }

  assert {
    condition     = google_cloud_run_v2_service.server.template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.ui.template[0].scaling[0].min_instance_count == 0
    error_message = "Both services should scale to zero by default."
  }

  assert {
    condition     = length(google_secret_manager_secret.oauth_client_secret) == 0 && length(google_secret_manager_secret.admin_password) == 0
    error_message = "Optional secrets should not be created when unset."
  }

  assert {
    condition     = length(google_monitoring_alert_policy.spike) == 0 && length(google_billing_budget.monthly) == 0 && length(google_storage_bucket.tf_state) == 0
    error_message = "Optional alerting, budget and state bucket should be off by default."
  }
}

run "allusers_with_drs_tag" {
  command = plan

  variables {
    public_access_mode = "allusers"
    drs_tag_value      = "tagValues/111111111111"
  }

  assert {
    condition     = !google_cloud_run_v2_service.server.invoker_iam_disabled
    error_message = "allusers mode must keep the invoker IAM check."
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.server_public[0].member == "allUsers" && google_cloud_run_v2_service_iam_member.ui_public[0].member == "allUsers"
    error_message = "allusers mode should bind allUsers on both services."
  }

  assert {
    condition     = google_tags_tag_binding.drs_exception[0].parent == "//cloudresourcemanager.googleapis.com/projects/123456789012"
    error_message = "DRS tag should be bound to the project by number."
  }
}

run "optional_features" {
  command = plan

  variables {
    server_admin_onboarding_id       = "admin@example.com"
    server_admin_onboarding_password = "hunter2hunter2"
    google_oauth_client_id           = "client-id"
    google_oauth_client_secret       = "client-secret"
    alert_email                      = "ops@example.com"
    billing_account_id               = "000000-000000-000000"
    create_state_bucket              = true
  }

  assert {
    condition     = length(google_secret_manager_secret.admin_password) == 1 && length(google_secret_manager_secret.oauth_client_secret) == 1
    error_message = "Admin password and OAuth secrets should be created when set."
  }

  assert {
    condition     = contains(keys(google_secret_manager_secret_iam_member.access), "server-admin-password") && contains(keys(google_secret_manager_secret_iam_member.access), "ui-oauth")
    error_message = "Optional secrets need per-secret accessor grants."
  }

  assert {
    condition     = length(google_monitoring_alert_policy.spike) == 3 && length(google_billing_budget.monthly) == 1
    error_message = "Alerts and budget should be created when configured."
  }

  assert {
    condition     = google_storage_bucket.tf_state[0].public_access_prevention == "enforced"
    error_message = "State bucket must enforce public access prevention."
  }
}

run "rejects_db_connection_overcommit" {
  command = plan

  variables {
    server_max_instances = 10
    server_db_pool_size  = 20
  }

  expect_failures = [google_cloud_run_v2_service.server]
}

run "rejects_bad_access_mode" {
  command = plan

  variables {
    public_access_mode = "public"
  }

  expect_failures = [var.public_access_mode]
}
