provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = merge(
    {
      app        = var.project_name
      managed-by = "terraform"
    },
    var.labels,
  )
}

locals {
  prefix = var.project_name

  # The server hardcodes the database name (server/server.py), so it is not
  # configurable here.
  db_name = "mpac"

  budget_enabled = var.billing_account_id != ""
  alerts_enabled = var.alert_email != ""
}

data "google_project" "main" {
  project_id = var.project_id
}

# APIs
#
# Enabled here so a brand-new project works with a single `terraform apply`.
# disable_on_destroy = false: tearing down MPAC should never switch off APIs
# that other workloads in the project may rely on.
resource "google_project_service" "services" {
  for_each = toset(concat(
    [
      "cloudresourcemanager.googleapis.com",
      "iam.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "sqladmin.googleapis.com",
    ],
    local.budget_enabled ? ["billingbudgets.googleapis.com"] : [],
  ))

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
