# Networking
#
# Two public subnets (ALB + the Fargate task) and two isolated subnets (RDS,
# no route to or from the internet) across two AZs.
#
# There is deliberately no NAT gateway (~$33/month plus data charges, more
# than the rest of the compute). The task instead gets a public IP for egress
# (model APIs, ghcr.io, AWS APIs). Its security group admits only the ALB, so
# nothing on the internet can connect to that IP. This is the AWS counterpart
# of the GCloud "public IP, no VPC connector" trade-off. There are no VPC
# interface endpoints for the same reason (~$7/month each).

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.prefix}-vpc" }
}

# CIS: the default security group must allow nothing.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-igw" }
}

resource "aws_subnet" "public" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  # ECS assigns the task's public IP explicitly; nothing else launches here.
  map_public_ip_on_launch = false

  tags = { Name = "${local.prefix}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "isolated" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)

  tags = { Name = "${local.prefix}-isolated-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Local routes only.
resource "aws_route_table" "isolated" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-isolated" }
}

resource "aws_route_table_association" "isolated" {
  count          = length(aws_subnet.isolated)
  subnet_id      = aws_subnet.isolated[count.index].id
  route_table_id = aws_route_table.isolated.id
}
