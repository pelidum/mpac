terraform {
  # 1.11+ for write-only arguments (secret_data_wo), which keep user-supplied
  # secrets out of state.
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
