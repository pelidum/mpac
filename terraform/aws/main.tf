provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        app        = var.project_name
        managed-by = "terraform"
      },
      var.tags,
    )
  }
}

locals {
  prefix = var.project_name

  # The server hardcodes the database name (server/server.py).
  db_name = "mpac"

  alerts_enabled = var.alert_email != ""
}

data "aws_caller_identity" "current" {}

# The ALB and the RDS subnet group both need subnets in two AZs.
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}
