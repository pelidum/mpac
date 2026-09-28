# ECS Fargate
#
# One service running one task with two containers. The UI reaches the server
# over localhost in plaintext (MPAC_GRPC_INSECURE), the same way on_prem's UI
# reaches the server over the private Docker network. That needs no service
# discovery or internal load balancer, and only one network interface and
# public IP. Both scale together, which suits a single MPAC deployment.

resource "aws_ecs_cluster" "main" {
  name = local.prefix

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.prefix}"
  retention_in_days = var.log_retention_days
}

locals {
  log_config = {
    for c in ["server", "ui"] : c => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.main.name
        awslogs-region        = var.region
        awslogs-stream-prefix = c
      }
    }
  }

  env = {
    # Changing secrets_version changes the task definition, which rolls the
    # service so containers pick up the rotated SSM values.
    MPAC_SECRETS_VERSION = tostring(var.secrets_version)
  }

  server_env = merge(
    local.env,
    {
      # asyncpg honours libpq's PGSSLMODE; RDS rejects non-TLS connections
      # (rds.force_ssl).
      PGSSLMODE             = "require"
      MPAC_MAX_MESSAGE_SIZE = tostring(var.server_max_message_size_mb * 1024 * 1024)
    },
    var.server_admin_onboarding_id != "" ? { MPAC_ADMIN_ONBOARDING_ID = var.server_admin_onboarding_id } : {},
    var.server_enable_reflection ? { MPAC_ENABLE_REFLECTION = "true" } : {},
  )

  server_secrets = merge(
    {
      MPAC_DB_PASSWORD = aws_ssm_parameter.db_password.arn
      MPAC_JWT_SECRET  = aws_ssm_parameter.jwt_secret.arn
    },
    local.create_admin_secret ? { MPAC_ADMIN_ONBOARDING_PASSWORD = aws_ssm_parameter.admin_password[0].arn } : {},
  )

  ui_env = merge(
    local.env,
    {
      MPAC_HOST          = "localhost"
      MPAC_PORT          = "50051"
      MPAC_GRPC_INSECURE = "true"
      MPAC_SYSTEM_USER   = var.ui_system_user
      # Only the ALB can reach the container (security group), and it always
      # sets X-Forwarded-For, so trust all sources, as on Cloud Run.
      MPAC_TRUSTED_PROXIES = "*"
    },
    local.oauth_enabled ? { GOOGLE_OAUTH_CLIENT_ID = var.google_oauth_client_id } : {},
    var.ui_debug ? { MPAC_DEBUG = "true" } : {},
  )

  ui_secrets = merge(
    { MPAC_JWT_SECRET = aws_ssm_parameter.jwt_secret.arn },
    local.oauth_enabled ? { GOOGLE_OAUTH_CLIENT_SECRET = aws_ssm_parameter.oauth_client_secret[0].arn } : {},
  )
}

resource "aws_ecs_task_definition" "main" {
  family                   = local.prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = one(aws_iam_role.task[*].arn)

  # The published images are x86_64 only (see server/BUILD, ui/BUILD).
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "server"
      image     = var.server_image
      essential = true
      command = [
        "--db_host=${aws_db_instance.main.address}",
        "--db_user=${var.postgres_user}",
        "--db_pool_size=${var.server_db_pool_size}",
        "--host=0.0.0.0",
        "--port=50051",
      ]
      portMappings = [{ containerPort = 50051, protocol = "tcp", name = "grpc", appProtocol = "grpc" }]
      environment  = [for k, v in local.server_env : { name = k, value = v }]
      secrets      = [for k, v in local.server_secrets : { name = k, valueFrom = v }]
      # The server marks itself NOT_SERVING and drains on SIGTERM. 120s is the
      # Fargate maximum. Runs cut off anyway resume on the next start.
      stopTimeout      = 120
      linuxParameters  = { initProcessEnabled = true }
      logConfiguration = local.log_config["server"]
    },
    {
      name             = "ui"
      image            = var.ui_image
      essential        = true
      portMappings     = [{ containerPort = 8080, protocol = "tcp", name = "http", appProtocol = "http" }]
      environment      = [for k, v in local.ui_env : { name = k, value = v }]
      secrets          = [for k, v in local.ui_secrets : { name = k, valueFrom = v }]
      dependsOn        = [{ containerName = "server", condition = "START" }]
      linuxParameters  = { initProcessEnabled = true }
      logConfiguration = local.log_config["ui"]
    },
  ])
}

resource "aws_ecs_service" "main" {
  name            = local.prefix
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main.arn
  desired_count   = 1

  # On-demand by default. Spot is ~70% cheaper, but AWS can reclaim the task
  # with 2 minutes' notice, leaving the site down until a replacement starts.
  # Interrupted runs resume on the next start (server startup resume +
  # periodic sweeper).
  capacity_provider_strategy {
    capacity_provider = var.use_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # egress without a NAT gateway; see network.tf
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ui.arn
    container_name   = "ui"
    container_port   = 8080
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.server.arn
    container_name   = "server"
    container_port   = 50051
  }

  # Start the new task before stopping the old one (zero-downtime deploys) and
  # roll back automatically if it never becomes healthy.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  health_check_grace_period_seconds = 120

  enable_execute_command  = var.enable_ecs_exec
  enable_ecs_managed_tags = true
  propagate_tags          = "SERVICE"

  lifecycle {
    precondition {
      # During a rolling deploy two tasks run at once, each with its own pool.
      # Leave headroom for RDS's reserved and admin connections.
      condition     = 2 * var.server_db_pool_size <= var.db_max_connections - 10
      error_message = "2 * server_db_pool_size must be <= db_max_connections - 10 (two tasks overlap during deploys). Lower server_db_pool_size or raise db_max_connections."
    }
  }

  depends_on = [
    aws_lb_listener.https_ui,
    aws_lb_listener.grpc,
    aws_iam_role_policy.execution,
    aws_ecs_cluster_capacity_providers.main,
  ]
}
