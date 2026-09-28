# Monitoring & cost guardrails
#
# Log-based metrics are free and always created. Alert policies, the email
# channel and the uptime check are created only when var.alert_email is set.

locals {
  cloud_run_services = "resource.labels.service_name=\"${google_cloud_run_v2_service.server.name}\" OR resource.labels.service_name=\"${google_cloud_run_v2_service.ui.name}\""

  alerts = {
    auth-failures = {
      display   = "Auth failures > 50 in 5 min"
      threshold = 50
    }
    login-failures = {
      display   = "Login failures > 20 in 5 min"
      threshold = 20
    }
    server-errors = {
      display   = "Server errors > 10 in 5 min"
      threshold = 10
    }
  }
}

resource "google_logging_metric" "auth_failures" {
  name   = "${local.prefix}-auth-failures"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    (${local.cloud_run_services})
    textPayload=~"AUTH: fail"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.services]
}

resource "google_logging_metric" "login_failures" {
  name   = "${local.prefix}-login-failures"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.server.name}"
    textPayload=~"Failed login attempt"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.services]
}

resource "google_logging_metric" "server_errors" {
  name   = "${local.prefix}-server-errors"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    (${local.cloud_run_services})
    severity>=ERROR
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.services]
}

resource "google_monitoring_notification_channel" "email" {
  count        = local.alerts_enabled ? 1 : 0
  display_name = "${local.prefix} alert email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "spike" {
  for_each = local.alerts_enabled ? local.alerts : {}

  display_name = "${local.prefix} ${each.key} spike"
  combiner     = "OR"
  conditions {
    display_name = each.value.display
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${local.prefix}-${each.key}\" AND resource.type=\"cloud_run_revision\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  # The filter names the metric by string, so order creation explicitly.
  depends_on = [
    google_logging_metric.auth_failures,
    google_logging_metric.login_failures,
    google_logging_metric.server_errors,
  ]
}

resource "google_monitoring_uptime_check_config" "ui_health" {
  count        = local.alerts_enabled ? 1 : 0
  display_name = "${local.prefix} UI health"
  timeout      = "10s"
  # Each check wakes the UI (request-billed, fractions of a cent). 5 minutes
  # keeps that negligible while still catching outages.
  period = "300s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(google_cloud_run_v2_service.ui.uri, "https://")
    }
  }
}

# Billing budget (optional)
#
# Emails billing admins (and var.alert_email if set) at 50/90/100% of
# var.monthly_budget_usd. Needs roles/billing.costsManager (or higher) on the
# billing account for whoever runs terraform apply.
resource "google_billing_budget" "monthly" {
  count           = local.budget_enabled ? 1 : 0
  billing_account = var.billing_account_id
  display_name    = "${local.prefix} monthly budget"

  budget_filter {
    projects = ["projects/${data.google_project.main.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }

  dynamic "all_updates_rule" {
    for_each = local.alerts_enabled ? [1] : []
    content {
      monitoring_notification_channels = [google_monitoring_notification_channel.email[0].id]
      disable_default_iam_recipients   = false
    }
  }

  depends_on = [google_project_service.services]
}
