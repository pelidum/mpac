locals {
  generate_secret_key = var.secret_key_url == "" && var.secret_key_path == ""
  secret_key_mount    = "/run/mpac/secret.key"
  secret_key_url      = var.secret_key_url != "" ? var.secret_key_url : "file://${local.secret_key_mount}"
  secret_key_file     = var.secret_key_url != "" ? "" : (var.secret_key_path != "" ? abspath(var.secret_key_path) : abspath("${path.module}/generated/secrets/master.key"))
  blob_env = [
    "MPAC_BLOB_URL=s3://blobs",
    "MPAC_BLOB_PUBLIC_ENDPOINT=${local.ui_origin}",
    "AWS_ENDPOINT=http://seaweedfs:8333",
    "AWS_ALLOW_HTTP=true",
    "AWS_REGION=us-east-1",
    "AWS_ACCESS_KEY_ID=${random_password.blob_access_key.result}",
    "AWS_SECRET_ACCESS_KEY=${random_password.blob_secret_key.result}",
  ]
  db_args = [
    "--db_host=postgres", "--db_name=${var.postgres_db}",
    "--db_user=${var.postgres_user}", "--db_pool_size=${var.server_db_pool_size}",
  ]
  server_env = concat(local.blob_env, [
    "MPAC_DB_PASSWORD=${var.postgres_password}",
    "MPAC_JWT_SECRET=${var.mpac_jwt_secret}",
    "MPAC_MAX_MESSAGE_SIZE=${var.server_max_message_size_mb * 1024 * 1024}",
    "MPAC_MAX_ATTACHMENT_SIZE=${var.max_attachment_size_mb * 1024 * 1024}",
    "MPAC_WORKER_CONCURRENCY=${var.worker_concurrency}",
    "MPAC_ENABLE_REFLECTION=${var.server_enable_reflection}",
    "MPAC_SECRET_KEY_URL=${local.secret_key_url}",
    ], var.secret_key_vault_token == "" ? [] : ["VAULT_TOKEN=${var.secret_key_vault_token}"]
  )
  backup_env = [
    "RCLONE_S3_PROVIDER=Other", "RCLONE_S3_ENV_AUTH=true",
    "RCLONE_S3_ENDPOINT=http://seaweedfs:8333", "RCLONE_S3_REGION=us-east-1",
    "RCLONE_S3_FORCE_PATH_STYLE=true",
    "AWS_ACCESS_KEY_ID=${random_password.blob_access_key.result}",
    "AWS_SECRET_ACCESS_KEY=${random_password.blob_secret_key.result}",
  ]
}

resource "random_bytes" "secret_key" {
  count  = local.generate_secret_key ? 1 : 0
  length = 32
}

resource "local_sensitive_file" "secret_key" {
  count                = local.generate_secret_key ? 1 : 0
  content              = random_bytes.secret_key[0].base64
  filename             = "${path.module}/generated/secrets/master.key"
  file_permission      = "0644"
  directory_permission = "0700"
}

resource "random_password" "blob_access_key" {
  length  = 20
  special = false
}

resource "random_password" "blob_secret_key" {
  length  = 40
  special = false
}

resource "local_sensitive_file" "seaweed_identity" {
  filename             = "${path.module}/generated/seaweed-s3.json"
  file_permission      = "0644"
  directory_permission = "0700"
  content = jsonencode({
    identities = [{
      name = "mpac"
      credentials = [{
        accessKey = random_password.blob_access_key.result
        secretKey = random_password.blob_secret_key.result
      }]
      actions = ["Read:blobs", "Write:blobs", "List:blobs", "Tagging:blobs"]
    }]
  })
}

resource "docker_image" "seaweedfs" {
  name         = var.seaweedfs_image
  keep_locally = true
}

resource "docker_container" "seaweedfs" {
  name  = "${var.project_name}-seaweedfs"
  image = docker_image.seaweedfs.image_id
  labels {
    label = "mpac.identity-sha256"
    value = nonsensitive(sha256(local_sensitive_file.seaweed_identity.content))
  }
  working_dir = "/data"
  command     = ["server", "-s3", "-ip=seaweedfs", "-ip.bind=0.0.0.0", "-dir=/data", "-master.volumeSizeLimitMB=128", "-s3.config=/etc/seaweedfs/s3.json"]
  volumes {
    host_path      = var.blob_data_path
    container_path = "/data"
  }
  volumes {
    host_path      = abspath(local_sensitive_file.seaweed_identity.filename)
    container_path = "/etc/seaweedfs/s3.json"
    read_only      = true
  }
  networks_advanced {
    name    = docker_network.app_network.name
    aliases = ["seaweedfs"]
  }
  healthcheck {
    test     = ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:9333/cluster/status && wget -q -O /dev/null http://127.0.0.1:8888/"]
    interval = "2s"
    timeout  = "2s"
    retries  = 60
  }
  wait         = true
  wait_timeout = 120
  restart      = "unless-stopped"
}

resource "docker_container" "seaweed_init" {
  name       = "${var.project_name}-blob-init"
  image      = docker_image.seaweedfs.image_id
  entrypoint = ["/bin/sh", "-ec"]
  command = [<<-SCRIPT
    for attempt in $(seq 1 60); do
      printf 's3.bucket.create -name blobs\nexit\n' | weed shell -master=seaweedfs:9333 -filer=seaweedfs:8888 || true
      if printf 's3.bucket.list\nexit\n' | weed shell -master=seaweedfs:9333 -filer=seaweedfs:8888 | grep -qw blobs; then exit 0; fi
      sleep 1
    done
    exit 1
  SCRIPT
  ]
  networks_advanced {
    name = docker_network.app_network.name
  }
  attach     = true
  must_run   = false
  depends_on = [docker_container.seaweedfs]
  lifecycle {
    replace_triggered_by = [docker_container.seaweedfs.id]
    postcondition {
      condition     = self.exit_code == 0
      error_message = "SeaweedFS bucket initialization failed; inspect the blob-init container logs."
    }
  }
}

resource "docker_container" "worker" {
  count      = var.worker_replicas
  name       = "${var.project_name}-worker-${count.index + 1}"
  image      = docker_image.server.image_id
  entrypoint = ["/usr/local/bin/mpac", "worker"]
  command    = local.db_args
  env        = concat(local.server_env, ["MPAC_ROLES=worker"])
  dynamic "volumes" {
    for_each = local.secret_key_file == "" ? [] : [local.secret_key_file]
    content {
      host_path      = volumes.value
      container_path = local.secret_key_mount
      read_only      = true
    }
  }
  networks_advanced {
    name = docker_network.app_network.name
  }
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  lifecycle {
    replace_triggered_by = [docker_container.server.id]
  }
  stop_timeout = 35
  restart      = "unless-stopped"
  depends_on   = [docker_container.server, local_sensitive_file.secret_key]
}

resource "docker_image" "rclone" {
  name         = var.rclone_image
  keep_locally = true
}

resource "docker_volume" "backup_tools" {
  name = "${var.project_name}-backup-tools"
}

resource "docker_container" "backup_tools" {
  name       = "${var.project_name}-backup-tools-init"
  image      = docker_image.rclone.image_id
  entrypoint = ["/bin/sh", "-ec"]
  command    = ["cp /usr/local/bin/rclone /tools/rclone"]
  volumes {
    volume_name    = docker_volume.backup_tools.name
    container_path = "/tools"
  }
  attach   = true
  must_run = false
  lifecycle {
    postcondition {
      condition     = self.exit_code == 0
      error_message = "Copying the backup binary failed; inspect backup-tools-init logs."
    }
  }
}
