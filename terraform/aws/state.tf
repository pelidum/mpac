# Remote state bucket (optional)
#
# The first apply uses local state, like on_prem. For anything long-lived, set
# create_state_bucket = true, apply, then move state into the bucket (see
# backend.tf.example). S3 native locking (use_lockfile) means no DynamoDB
# table is needed.

resource "aws_s3_bucket" "tf_state" {
  count         = var.create_state_bucket ? 1 : 0
  bucket_prefix = "${local.prefix}-tfstate-"
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "tf_state" {
  count  = var.create_state_bucket ? 1 : 0
  bucket = aws_s3_bucket.tf_state[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  count                   = var.create_state_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.tf_state[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "tf_state" {
  count  = var.create_state_bucket ? 1 : 0
  bucket = aws_s3_bucket.tf_state[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  count  = var.create_state_bucket ? 1 : 0
  bucket = aws_s3_bucket.tf_state[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Keep the last few versions for rollback without paying to keep every one.
resource "aws_s3_bucket_lifecycle_configuration" "tf_state" {
  count  = var.create_state_bucket ? 1 : 0
  bucket = aws_s3_bucket.tf_state[0].id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days           = 30
      newer_noncurrent_versions = 3
    }
  }

  depends_on = [aws_s3_bucket_versioning.tf_state]
}

resource "aws_s3_bucket_policy" "tf_state" {
  count  = var.create_state_bucket ? 1 : 0
  bucket = aws_s3_bucket.tf_state[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.tf_state[0].arn,
        "${aws_s3_bucket.tf_state[0].arn}/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.tf_state]
}
