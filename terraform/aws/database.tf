# RDS PostgreSQL
#
# db.t4g.micro is the cheapest instance class and plenty for MPAC. (Graviton is
# fine here: the database's architecture has nothing to do with the x86 app
# images.) Single-AZ: Multi-AZ doubles the cost.
# Not publicly accessible, in subnets with no internet route, reachable only
# from the task's security group, and TLS is enforced.

resource "aws_db_subnet_group" "main" {
  name       = "${local.prefix}-db"
  subnet_ids = aws_subnet.isolated[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.prefix}-postgres16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name         = "max_connections"
    value        = var.db_max_connections
    apply_method = "pending-reboot"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Final snapshot names must be unique, so a second destroy/recreate cycle
# doesn't collide with the first one's snapshot.
resource "random_id" "final_snapshot" {
  byte_length = 4
}

resource "aws_db_instance" "main" {
  identifier     = "${local.prefix}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name             = local.db_name
  username            = var.postgres_user
  password_wo         = local.postgres_password
  password_wo_version = var.secrets_version

  storage_type          = "gp3"
  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:30-sun:05:30"
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.prefix}-db-final-${random_id.final_snapshot.hex}"
}
