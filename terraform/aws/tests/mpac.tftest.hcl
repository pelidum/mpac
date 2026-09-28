# Offline tests. The AWS provider is mocked, so no credentials are needed and
# nothing is created, even by the `apply` runs (mocks fill in computed values). random and tls run for real (they're local-only).
# Run with: terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }

  mock_resource "aws_lb" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mpac-alb/0123456789abcdef"
      arn_suffix = "app/mpac-alb/0123456789abcdef"
      dns_name   = "mpac-alb-123456789.us-east-1.elb.amazonaws.com"
      zone_id    = "Z35SXDOTRQ7X7K"
    }
  }

  mock_resource "aws_lb_target_group" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mpac/0123456789abcdef"
      arn_suffix = "targetgroup/mpac/0123456789abcdef"
    }
  }

  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:123456789012:mpac-alerts"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::mpac-tfstate-mock"
    }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mpac:1"
    }
  }

  mock_resource "aws_ecs_cluster" {
    defaults = {
      arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mpac"
    }
  }

  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
      domain_validation_options = [{
        domain_name           = "mpac.example.com"
        resource_record_name  = "_abc.mpac.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def.acm-validations.aws."
      }]
    }
  }

  mock_resource "aws_ssm_parameter" {
    defaults = {
      arn = "arn:aws:ssm:us-east-1:123456789012:parameter/mpac/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/mpac"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }
}

variables {
  region = "us-east-1"
}

run "defaults" {
  command = apply

  assert {
    condition     = output.certificate_mode == "self_signed" && length(aws_acm_certificate.self_signed) == 1 && length(aws_acm_certificate.domain) == 0
    error_message = "With no domain, a self-signed certificate should be imported into ACM."
  }

  assert {
    condition     = aws_lb_target_group.server.protocol_version == "GRPC" && aws_lb_target_group.server.health_check[0].path == "/grpc.health.v1.Health/Check" && aws_lb_target_group.server.health_check[0].matcher == "0"
    error_message = "Server target group must be gRPC with the standard health check."
  }

  assert {
    condition     = aws_lb_listener.https_ui.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06" && aws_lb_listener.grpc.port == 50051
    error_message = "HTTPS listeners must use the TLS 1.2/1.3 policy, with gRPC on 50051."
  }

  assert {
    condition     = aws_lb_listener.http_redirect.default_action[0].type == "redirect" && aws_lb_listener.http_redirect.default_action[0].redirect[0].protocol == "HTTPS"
    error_message = "Port 80 must only redirect to HTTPS."
  }

  assert {
    # (Mocks fill unset optional attributes with random strings, so check that
    # the HSTS value was not configured rather than that it is null.)
    condition     = aws_lb_listener.https_ui.routing_http_response_strict_transport_security_header_value != "max-age=31536000; includeSubDomains"
    error_message = "No HSTS on a self-signed certificate."
  }

  assert {
    condition     = !aws_db_instance.main.publicly_accessible && aws_db_instance.main.storage_encrypted && aws_db_instance.main.instance_class == "db.t4g.micro"
    error_message = "RDS must be private, encrypted, and the cheapest class by default."
  }

  assert {
    condition     = one([for p in aws_db_parameter_group.main.parameter : p.value if p.name == "rds.force_ssl"]) == "1"
    error_message = "RDS must force TLS."
  }

  assert {
    condition     = alltrue([for r in aws_vpc_security_group_ingress_rule.task_from_alb : r.referenced_security_group_id == aws_security_group.alb.id && r.cidr_ipv4 == null])
    error_message = "The task may only be reachable from the ALB security group."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.db_from_task.referenced_security_group_id == aws_security_group.task.id
    error_message = "The database may only be reachable from the task security group."
  }

  assert {
    condition     = length(aws_subnet.public) == 2 && length(aws_subnet.isolated) == 2
    error_message = "Two AZs of public and isolated subnets expected."
  }

  assert {
    condition     = one(aws_ecs_service.main.capacity_provider_strategy).capacity_provider == "FARGATE"
    error_message = "On-demand Fargate by default."
  }

  assert {
    condition     = aws_ecs_task_definition.main.runtime_platform[0].cpu_architecture == "X86_64"
    error_message = "Images are x86_64 only."
  }

  assert {
    condition     = length(aws_iam_role.task) == 0 && !aws_ecs_service.main.enable_execute_command
    error_message = "No task role or ECS Exec by default."
  }

  assert {
    condition     = length(aws_ssm_parameter.oauth_client_secret) == 0 && length(aws_ssm_parameter.admin_password) == 0
    error_message = "Optional secrets should not be created when unset."
  }

  assert {
    condition     = length(aws_sns_topic.alerts) == 0 && length(aws_budgets_budget.monthly) == 0 && length(aws_s3_bucket.tf_state) == 0
    error_message = "Alerts, budget and state bucket should be off by default."
  }
}

run "container_wiring" {
  command = apply

  variables {
    server_admin_onboarding_id       = "admin@example.com"
    server_admin_onboarding_password = "hunter2hunter2"
  }

  assert {
    condition = alltrue([
      for c in jsondecode(aws_ecs_task_definition.main.container_definitions) :
      contains([for e in c.environment : e.name], "MPAC_SECRETS_VERSION")
    ])
    error_message = "Both containers must carry MPAC_SECRETS_VERSION so rotation redeploys them."
  }

  assert {
    condition = anytrue([
      for c in jsondecode(aws_ecs_task_definition.main.container_definitions) :
      c.name == "ui" && contains([for e in c.environment : "${e.name}=${e.value}"], "MPAC_GRPC_INSECURE=true") && contains([for e in c.environment : "${e.name}=${e.value}"], "MPAC_HOST=localhost")
    ])
    error_message = "UI must reach the server on localhost in plaintext."
  }

  assert {
    condition = anytrue([
      for c in jsondecode(aws_ecs_task_definition.main.container_definitions) :
      c.name == "server" && contains([for s in c.secrets : s.name], "MPAC_ADMIN_ONBOARDING_PASSWORD") && contains([for e in c.environment : "${e.name}=${e.value}"], "PGSSLMODE=require")
    ])
    error_message = "Server must get the admin password from SSM and require TLS to the database."
  }

  assert {
    condition = alltrue([
      for c in jsondecode(aws_ecs_task_definition.main.container_definitions) :
      alltrue([for e in c.environment : !can(regex("(PASSWORD|SECRET)$", e.name))])
    ])
    error_message = "No secret may be passed as a plain environment variable."
  }
}

run "route53_domain" {
  command = apply

  variables {
    domain_name     = "mpac.example.com"
    route53_zone_id = "Z0123456789ABCDEFGHIJ"
  }

  assert {
    condition     = output.certificate_mode == "route53" && length(aws_acm_certificate.domain) == 1 && length(aws_acm_certificate.self_signed) == 0 && length(aws_route53_record.endpoint) == 1
    error_message = "A domain with a Route 53 zone should get an ACM certificate and alias record."
  }

  assert {
    condition     = output.ui_url == "https://mpac.example.com" && output.grpc_endpoint == "mpac.example.com:50051"
    error_message = "Endpoints should use the custom domain."
  }

  assert {
    condition     = aws_lb_listener.https_ui.routing_http_response_strict_transport_security_header_value == "max-age=31536000; includeSubDomains"
    error_message = "HSTS should be on with a trusted certificate."
  }
}

run "byo_certificate" {
  command = apply

  variables {
    domain_name         = "mpac.example.com"
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-1111-1111-1111-111111111111"
  }

  assert {
    condition     = output.certificate_mode == "byo" && length(aws_acm_certificate.domain) == 0 && length(aws_acm_certificate.self_signed) == 0 && length(aws_route53_record.endpoint) == 0
    error_message = "A supplied certificate should create no certificate or DNS records."
  }

  assert {
    condition     = aws_lb_listener.grpc.certificate_arn == var.acm_certificate_arn
    error_message = "Listeners should use the supplied certificate."
  }
}

run "optional_features" {
  command = apply

  variables {
    use_spot               = true
    enable_ecs_exec        = true
    google_oauth_client_id = "client-id"
    alert_email            = "ops@example.com"
    create_state_bucket    = true
  }

  assert {
    condition     = one(aws_ecs_service.main.capacity_provider_strategy).capacity_provider == "FARGATE_SPOT"
    error_message = "use_spot should switch to Fargate Spot."
  }

  assert {
    condition     = length(aws_iam_role.task) == 1 && aws_ecs_service.main.enable_execute_command
    error_message = "ECS Exec needs a task role."
  }

  assert {
    condition     = length(aws_ssm_parameter.oauth_client_secret) == 1
    error_message = "OAuth secret parameter should exist when OAuth is configured."
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.log_spike) == 3 && length(aws_cloudwatch_metric_alarm.unhealthy) == 2 && length(aws_budgets_budget.monthly) == 1
    error_message = "Alarms and budget should be created with alert_email."
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.tf_state[0].block_public_policy && aws_s3_bucket_ownership_controls.tf_state[0].rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "State bucket must block public access and disable ACLs."
  }
}

run "rejects_db_connection_overcommit" {
  command = plan

  variables {
    server_db_pool_size = 50
  }

  expect_failures = [aws_ecs_service.main]
}

run "rejects_domain_without_certificate_source" {
  command = plan

  variables {
    domain_name = "mpac.example.com"
  }

  expect_failures = [var.route53_zone_id]
}
