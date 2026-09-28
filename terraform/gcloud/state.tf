# Remote state bucket (optional)
#
# The first apply uses local state, the same as on_prem, so nothing is needed
# up front. For anything long-lived, set create_state_bucket = true, apply,
# then move state into the bucket (see backend.tf.example and README.md).
# State contains generated secrets, so the bucket is private and versioned.

resource "random_id" "state_suffix" {
  count       = var.create_state_bucket ? 1 : 0
  byte_length = 4
}

resource "google_storage_bucket" "tf_state" {
  count    = var.create_state_bucket ? 1 : 0
  name     = "${var.project_id}-${local.prefix}-tfstate-${random_id.state_suffix[0].hex}"
  location = var.region

  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Keep the last few versions for rollback without paying to keep every one.
  lifecycle_rule {
    condition {
      num_newer_versions = 3
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}
