# Monitoring & cost guardrails
#
# Log metric filters are always created (free until they match). Alarms, the
# SNS topic and the budget are created only when var.alert_email is set.
# Thresholds match terraform/gcloud.

locals {
  metric_namespace = "MPAC/${local.prefix}"

  log_alerts = {
    auth-failures = {
      pattern   = "\"AUTH: fail\""
      threshold = 50
    }
    login-failures = {
      pattern   = "\"Failed login attempt\""
      threshold = 20
    }
    # Python logging ("ERROR:...") and absl ("E0928 ...") error lines, plus
    # uncaught exceptions.
    server-errors = {
      pattern   = "?ERROR ?Traceback"
      threshold = 10
    }
  }

  alarm_actions = aws_sns_topic.alerts[*].arn
}

# Not encrypted with KMS: CloudWatch alarms can't publish to a topic that uses
# the AWS-managed SNS key, and a customer-managed key costs $1/month. The topic
# only carries alarm notifications.
resource "aws_sns_topic" "alerts" {
  count = local.alerts_enabled ? 1 : 0
  name  = "${local.prefix}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = local.alerts_enabled ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_log_metric_filter" "alerts" {
  for_each       = local.log_alerts
  name           = "${local.prefix}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.main.name
  pattern        = each.value.pattern

  metric_transformation {
    name      = each.key
    namespace = local.metric_namespace
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "log_spike" {
  for_each = local.alerts_enabled ? local.log_alerts : {}

  alarm_name          = "${local.prefix}-${each.key}-spike"
  alarm_description   = "More than ${each.value.threshold} ${each.key} in 5 minutes"
  namespace           = local.metric_namespace
  metric_name         = each.key
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.alerts]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy" {
  for_each = local.alerts_enabled ? {
    ui     = aws_lb_target_group.ui.arn_suffix
    server = aws_lb_target_group.server.arn_suffix
  } : {}

  alarm_name          = "${local.prefix}-${each.key}-unhealthy"
  alarm_description   = "MPAC ${each.key} has had no healthy target for 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  count = local.alerts_enabled ? 1 : 0

  alarm_name          = "${local.prefix}-alb-5xx"
  alarm_description   = "More than 10 load balancer 5xx responses in 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  count = local.alerts_enabled ? 1 : 0

  alarm_name          = "${local.prefix}-db-storage-low"
  alarm_description   = "Less than 2 GB free on the database (storage autoscaling may be at its cap)"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 2 * 1024 * 1024 * 1024
  alarm_actions       = local.alarm_actions

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }
}

# Account-wide monthly budget (the first two budgets per account are free).
# Emails at 50% and 90% of actual spend and when the forecast exceeds 100%.
resource "aws_budgets_budget" "monthly" {
  count        = local.alerts_enabled ? 1 : 0
  name         = "${local.prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = {
      "50"  = "ACTUAL"
      "90"  = "ACTUAL"
      "100" = "FORECASTED"
    }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = tonumber(notification.key)
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
