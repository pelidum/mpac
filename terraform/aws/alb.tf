# Application Load Balancer
#
# Plays the role Envoy plays on-prem (terraform/on_prem/envoy.yaml), on the same
# ports so clients connect the same way:
#   :80    → redirect to HTTPS
#   :443   → UI
#   :50051 → gRPC server
# App Runner and API Gateway can't carry gRPC, so an ALB is the cheapest
# managed option. It is also the largest fixed cost here.

resource "aws_lb" "main" {
  name               = "${local.prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Benchmarks and long runs hold requests open. Envoy runs with no idle
  # timeout on-prem; the ALB maximum is 4000s, and 3600s matches Cloud Run.
  idle_timeout = 3600

  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "ui" {
  name                 = "${local.prefix}-ui"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  protocol             = "HTTP"
  port                 = 8080
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# gRPC to the container over plaintext HTTP/2 (h2c), exactly as Envoy's
# upstream cluster does on-prem. TLS terminates at the ALB.
resource "aws_lb_target_group" "server" {
  name                 = "${local.prefix}-grpc"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  protocol             = "HTTP"
  protocol_version     = "GRPC"
  port                 = 50051
  deregistration_delay = 30

  # The server registers the standard gRPC health service (server/server.py).
  health_check {
    path                = "/grpc.health.v1.Health/Check"
    matcher             = "0"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https_ui" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.tls_policy
  certificate_arn   = local.certificate_arn

  # Same header Envoy adds on-prem. HSTS only with a publicly trusted cert: on
  # a self-signed one it would pin browsers to an endpoint they already warn on.
  routing_http_response_content_security_policy_header_value = "upgrade-insecure-requests"
  routing_http_response_strict_transport_security_header_value = (
    local.trusted_cert ? "max-age=31536000; includeSubDomains" : null
  )

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui.arn
  }
}

resource "aws_lb_listener" "grpc" {
  load_balancer_arn = aws_lb.main.arn
  port              = 50051
  protocol          = "HTTPS"
  ssl_policy        = var.tls_policy
  certificate_arn   = local.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.server.arn
  }
}
