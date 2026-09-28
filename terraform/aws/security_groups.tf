# Security groups: internet → ALB → task → database, each hop only from the one
# before it.

locals {
  alb_listener_ports = [80, 443, 50051]
  task_ports = {
    ui     = 8080
    server = 50051
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "MPAC load balancer"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each = {
    for pair in setproduct(local.alb_listener_ports, var.allowed_ingress_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = pair[0], cidr = pair[1] }
  }

  security_group_id = aws_security_group.alb.id
  description       = "Listener ${each.value.port}"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "alb_to_task" {
  for_each = local.task_ports

  security_group_id            = aws_security_group.alb.id
  description                  = "To MPAC ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  referenced_security_group_id = aws_security_group.task.id
}

resource "aws_security_group" "task" {
  name        = "${local.prefix}-task"
  description = "MPAC Fargate task"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  for_each = local.task_ports

  security_group_id            = aws_security_group.task.id
  description                  = "MPAC ${each.key} from the ALB only"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  referenced_security_group_id = aws_security_group.alb.id
}

# Outbound is open: inference backends are user-registered at runtime and can
# live on any host/port (OpenRouter, OpenAI, self-hosted endpoints).
resource "aws_vpc_security_group_egress_rule" "task_all" {
  security_group_id = aws_security_group.task.id
  description       = "Model APIs, image pulls, AWS APIs"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "db" {
  name        = "${local.prefix}-db"
  description = "MPAC PostgreSQL"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_task" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the MPAC task only"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.task.id
}
