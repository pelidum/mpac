# IAM
#
# The execution role is what ECS itself uses to write logs and inject secrets.
# The task role is what the app uses: attachment storage, plus ECS Exec if enabled.

locals {
  ssm_parameter_arns = concat(
    [aws_ssm_parameter.db_password.arn, aws_ssm_parameter.jwt_secret.arn],
    aws_ssm_parameter.oauth_client_secret[*].arn,
    aws_ssm_parameter.admin_password[*].arn,
  )

  ecs_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy protection: only tasks in this account.
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-ecs-execution"
  assume_role_policy = local.ecs_assume_role
}

# Scoped to this deployment's log group and parameters. The parameters use the
# AWS-managed aws/ssm key, which needs no extra KMS grant.
resource "aws_iam_role_policy" "execution" {
  name = "logs-and-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.main.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = local.ssm_parameter_arns
      },
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.prefix}-ecs-task"
  assume_role_policy = local.ecs_assume_role
}

resource "aws_iam_role_policy" "task_blobs" {
  name = "attachment-storage"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.blobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.blobs.arn}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "task_master_key" {
  name = "provider-key-master-key"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:Encrypt"]
      Resource = aws_kms_key.master.arn
    }]
  })
}

resource "aws_iam_role_policy" "task_exec" {
  count = var.enable_ecs_exec ? 1 : 0
  name  = "ecs-exec"
  role  = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}
