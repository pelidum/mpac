mock_provider "docker" {}
mock_provider "random" {}
mock_provider "tls" {}
mock_provider "local" {}

variables {
  postgres_password = "test-password"
  mpac_jwt_secret   = "test-secret-for-plans-only"
}

run "embedded" {
  command = plan
  assert {
    condition     = contains(docker_container.server.env, "MPAC_ROLES=api,worker") && length(docker_container.worker) == 0
    error_message = "Default deployment must embed the worker."
  }
  assert {
    condition     = contains(docker_container.server.env, "MPAC_BLOB_URL=s3://blobs") && contains(docker_container.server.env, "AWS_ENDPOINT=http://seaweedfs:8333")
    error_message = "Server must use the private SeaweedFS endpoint."
  }
  assert {
    condition     = contains(docker_container.server.env, "MPAC_BLOB_PUBLIC_ENDPOINT=https://localhost") && contains(docker_container.ui.env, "MPAC_BLOB_INTERNAL_ENDPOINT=http://seaweedfs:8333")
    error_message = "Browser URLs must use Envoy; UI transfers must reach the private endpoint."
  }
  assert {
    condition     = length(docker_container.seaweedfs.ports) == 0 && length(docker_container.postgres.ports) == 0
    error_message = "Storage must remain private."
  }
  assert {
    condition     = docker_container.seaweedfs.working_dir == "/data" && docker_container.seaweedfs.wait && docker_container.postgres.wait
    error_message = "Persist filer metadata and wait for storage readiness."
  }
  assert {
    condition     = strcontains(local_file.envoy_config.content, "prefix: \"/blobs/\"") && !strcontains(local_file.envoy_config.content, "prefix_rewrite")
    error_message = "Blob routing must preserve signed paths."
  }
  assert {
    condition     = docker_container.postgres_backup.entrypoint == tolist(["/bin/bash", "/etc/mpac/backup.sh"])
    error_message = "Backups must use the coordinated DB/blob backup script."
  }
}

run "separate_workers_custom_ports" {
  command = plan
  variables {
    worker_replicas    = 2
    worker_concurrency = 3
    envoy_https_port   = 8443
    postgres_db        = "custom_mpac"
  }
  assert {
    condition     = contains(docker_container.server.env, "MPAC_ROLES=api") && length(docker_container.worker) == 2
    error_message = "Separate mode must have an API-only server and requested workers."
  }
  assert {
    condition     = alltrue([for worker in docker_container.worker : contains(worker.env, "MPAC_ROLES=worker") && contains(worker.env, "MPAC_WORKER_CONCURRENCY=3") && contains(worker.env, "MPAC_BLOB_URL=s3://blobs")])
    error_message = "Workers must share concurrency, auth and storage configuration."
  }
  assert {
    condition     = contains(docker_container.server.env, "MPAC_BLOB_PUBLIC_ENDPOINT=https://localhost:8443") && strcontains(local_file.envoy_config.content, "port_redirect: 8443")
    error_message = "Signed URLs and redirects must respect nonstandard HTTPS ports."
  }
  assert {
    condition     = contains(docker_container.server.command, "--db_name=custom_mpac") && alltrue([for worker in docker_container.worker : contains(worker.command, "--db_name=custom_mpac")])
    error_message = "All processes must use the configured database name."
  }
}

run "reject_fractional_workers" {
  command = plan
  variables { worker_replicas = 1.5 }
  expect_failures = [var.worker_replicas]
}

run "generated_master_key_mounts_into_the_server_only" {
  command = apply

  assert {
    condition     = contains(docker_container.server.env, "MPAC_SECRET_KEY_URL=file:///run/mpac/secret.key")
    error_message = "The server must read the generated master key."
  }
  assert {
    condition     = anytrue([for volume in docker_container.server.volumes : volume.container_path == "/run/mpac/secret.key" && volume.read_only])
    error_message = "The master key must be mounted read-only into the server."
  }
  assert {
    condition     = !anytrue([for volume in docker_container.postgres_backup.volumes : strcontains(coalesce(volume.host_path, "-"), "secrets")])
    error_message = "The backup container must not receive the master key."
  }
  assert {
    condition     = !anytrue([for env in docker_container.postgres_backup.env : startswith(env, "MPAC_SECRET_KEY_URL")])
    error_message = "The backup container must not know the master key location."
  }
}

run "external_master_key_needs_no_file" {
  command = apply

  variables {
    secret_key_url         = "vault+https://vault.example.test/transit/mpac"
    secret_key_vault_token = "token"
  }

  assert {
    condition     = length(local_sensitive_file.secret_key) == 0
    error_message = "An external master key must not generate a key file."
  }
  assert {
    condition     = contains(docker_container.server.env, "MPAC_SECRET_KEY_URL=vault+https://vault.example.test/transit/mpac")
    error_message = "The server must use the external master key."
  }
  assert {
    condition     = contains(docker_container.server.env, "VAULT_TOKEN=token")
    error_message = "The server must receive the Vault token."
  }
}
