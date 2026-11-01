resource "google_storage_bucket" "blobs" {
  name                        = "${var.project_id}-${local.prefix}-blobs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection

  lifecycle_rule {
    condition {
      age            = 2
      matches_prefix = ["tmp/", "staging/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "server_blobs" {
  bucket = google_storage_bucket.blobs.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.server.member
}

resource "google_service_account_iam_member" "server_signs_urls" {
  service_account_id = google_service_account.server.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.server.member
}

resource "google_kms_key_ring" "main" {
  name       = local.prefix
  location   = var.region
  depends_on = [google_project_service.services]
}

resource "google_kms_crypto_key" "master" {
  name            = "${local.prefix}-master"
  key_ring        = google_kms_key_ring.main.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "server_master_key" {
  crypto_key_id = google_kms_crypto_key.master.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = google_service_account.server.member
}

resource "google_project_iam_audit_config" "kms" {
  project = var.project_id
  service = "cloudkms.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
