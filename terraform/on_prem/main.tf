terraform {
  required_version = ">= 1.0"
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.0"
    }
  }
}

provider "docker" {}

# TLS certificate
#
# By default MPAC generates a self-signed cert so `terraform apply` works with
# zero prep. To bring your own cert, set var.tls_cert_path / var.tls_key_path
# and generation is skipped automatically.
locals {
  generate_cert = var.tls_cert_path == ""
  envoy_cert    = local.generate_cert ? abspath("${path.module}/generated/envoy.crt") : var.tls_cert_path
  envoy_key     = local.generate_cert ? abspath("${path.module}/generated/envoy.key") : var.tls_key_path
  envoy_config  = var.envoy_config_path != "" ? abspath(var.envoy_config_path) : abspath("${path.module}/envoy.yaml")
}

resource "tls_private_key" "envoy" {
  count     = local.generate_cert ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "envoy" {
  count           = local.generate_cert ? 1 : 0
  private_key_pem = tls_private_key.envoy[0].private_key_pem

  subject {
    common_name  = var.tls_domain
    organization = var.project_name
  }

  dns_names    = distinct([var.tls_domain, "localhost"])
  ip_addresses = ["127.0.0.1"]

  validity_period_hours = var.tls_validity_days * 24

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

resource "local_file" "envoy_cert" {
  count           = local.generate_cert ? 1 : 0
  content         = tls_self_signed_cert.envoy[0].cert_pem
  filename        = "${path.module}/generated/envoy.crt"
  file_permission = "0644"
}

resource "local_sensitive_file" "envoy_key" {
  count   = local.generate_cert ? 1 : 0
  content = tls_private_key.envoy[0].private_key_pem
  # World-readable (0644): Envoy mounts this read-only, and under rootless /
  # userns-remapped Docker the container user maps to an unprivileged host uid
  # that cannot read an owner-only (0600) file. The key is a self-signed cert
  # for local use and lives in the git-ignored generated/ dir.
  filename        = "${path.module}/generated/envoy.key"
  file_permission = "0644"
}

# Shared network for all containers
resource "docker_network" "app_network" {
  name = "${var.project_name}-network"
}

# PostgreSQL
resource "docker_image" "postgres" {
  name         = "postgres:${var.postgres_version}"
  keep_locally = true
}

resource "docker_container" "postgres" {
  name  = "${var.project_name}-postgres"
  image = docker_image.postgres.image_id

  env = [
    "POSTGRES_PASSWORD=${var.postgres_password}",
    "POSTGRES_DB=${var.postgres_db}",
    "POSTGRES_USER=${var.postgres_user}",
  ]

  # No external ports - only accessible within Docker network

  volumes {
    host_path      = var.postgres_data_path
    container_path = "/var/lib/postgresql/data"
  }

  networks_advanced {
    name    = docker_network.app_network.name
    aliases = ["postgres", "db"]
  }

  restart = "unless-stopped"
}

# PostgreSQL backup — runs pg_dump on a cron schedule and prunes old dumps.
# Uses a sleep loop since the base postgres image doesn't include cron.
resource "docker_container" "postgres_backup" {
  name  = "${var.project_name}-postgres-backup"
  image = docker_image.postgres.image_id

  env = [
    "PGPASSWORD=${var.postgres_password}",
  ]

  entrypoint = ["/bin/bash", "-c"]
  command = [<<-EOT
    while true; do
      echo "[$(date)] Starting pg_dump..."
      pg_dump -h postgres -U ${var.postgres_user} -d ${var.postgres_db} -Fc \
        -f /backups/${var.postgres_db}_$(date +%Y%m%d_%H%M%S).dump
      echo "[$(date)] Pruning backups older than ${var.postgres_backup_retention_days} days..."
      find /backups -name '*.dump' -mtime +${var.postgres_backup_retention_days} -delete
      echo "[$(date)] Backup complete. Sleeping 24h..."
      sleep 86400
    done
  EOT
  ]

  volumes {
    host_path      = var.postgres_backup_path
    container_path = "/backups"
  }

  networks_advanced {
    name = docker_network.app_network.name
  }

  restart = "unless-stopped"

  depends_on = [docker_container.postgres]
}

# MPAC Server
resource "docker_image" "server" {
  name         = var.server_image
  keep_locally = true
}

resource "docker_container" "server" {
  name  = "${var.project_name}-server"
  image = docker_image.server.image_id

  env = concat(
    [
      "MPAC_JWT_SECRET=${var.mpac_jwt_secret}",
      "MPAC_MAX_MESSAGE_SIZE=${var.server_max_message_size_mb * 1024 * 1024}",
    ],
    var.server_enable_reflection ? ["MPAC_ENABLE_REFLECTION=true"] : [],
  )

  command = concat(
    [
      "--db_host=postgres",
      "--db_user=${var.postgres_user}",
      "--db_password=${var.postgres_password}",
      "--db_pool_size=${var.server_db_pool_size}",
      "--host=${var.server_host}",
      "--port=${var.server_port}",
    ],
    var.server_admin_onboarding_id != "" ? ["--admin_onboarding_id=${var.server_admin_onboarding_id}"] : [],
    var.server_admin_onboarding_password != "" ? ["--admin_onboarding_password=${var.server_admin_onboarding_password}"] : [],
  )

  # No external ports - only accessible via Envoy

  networks_advanced {
    name    = docker_network.app_network.name
    aliases = ["server", "mpac-server"]
  }

  # Allow container to access host machine services (e.g., Ollama on localhost:11434).
  # host.docker.internal → host gateway for explicit use.
  # localhost → host gateway so that backend URLs like http://localhost:11434/v1 work
  # from inside the container without changing how Ollama is started.
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  host {
    host = "localhost"
    ip   = "host-gateway"
  }

  restart = "unless-stopped"

  depends_on = [docker_container.postgres]
}

# MPAC UI
resource "docker_image" "ui" {
  name         = var.ui_image
  keep_locally = true
}

resource "docker_container" "ui" {
  name  = "${var.project_name}-ui"
  image = docker_image.ui.image_id

  env = concat(
    [
      "GOOGLE_OAUTH_CLIENT_SECRET=${var.google_oauth_client_secret}",
      "GOOGLE_OAUTH_CLIENT_ID=${var.google_oauth_client_id}",
      "MPAC_JWT_SECRET=${var.mpac_jwt_secret}",
      "MPAC_SYSTEM_USER=${var.ui_system_user}",
      "MPAC_PORT=${var.server_port}",
      "MPAC_HOST=${var.project_name}-server",
      "MPAC_TRUSTED_PROXIES=${var.ui_trusted_proxies}",
    ],
    var.ui_grpc_insecure ? ["MPAC_GRPC_INSECURE=true"] : [],
    var.ui_debug ? ["MPAC_DEBUG=true"] : [],
  )

  # No external ports - only accessible via Envoy

  networks_advanced {
    name    = docker_network.app_network.name
    aliases = ["ui", "mpac-ui"]
  }

  restart = "unless-stopped"

  depends_on = [docker_container.server]
}

# Envoy Proxy
resource "docker_image" "envoy" {
  name         = var.envoy_image
  keep_locally = true
}

resource "docker_container" "envoy" {
  name  = "${var.project_name}-envoy"
  image = docker_image.envoy.image_id

  # HTTP -> HTTPS redirect
  ports {
    internal = 8080
    external = var.envoy_http_port
    ip       = var.envoy_bind_ip
  }

  # HTTPS for UI
  ports {
    internal = 8443
    external = var.envoy_https_port
    ip       = var.envoy_bind_ip
  }

  # gRPC API (TLS)
  ports {
    internal = 50051
    external = var.envoy_grpc_port
    ip       = var.envoy_bind_ip
  }

  # Envoy admin (optional, commented out by default)
  # ports {
  #   internal = 9901
  #   external = 9901
  #   ip       = var.envoy_bind_ip
  # }

  volumes {
    host_path      = local.envoy_config
    container_path = "/etc/envoy/envoy.yaml"
    read_only      = true
  }

  volumes {
    host_path      = local.envoy_cert
    container_path = "/etc/envoy/envoy.crt"
    read_only      = true
  }

  volumes {
    host_path      = local.envoy_key
    container_path = "/etc/envoy/envoy.key"
    read_only      = true
  }

  command = ["envoy", "-c", "/etc/envoy/envoy.yaml"]

  networks_advanced {
    name    = docker_network.app_network.name
    aliases = ["envoy", "proxy"]
  }

  restart = "unless-stopped"

  depends_on = [
    docker_container.server,
    docker_container.ui,
    local_file.envoy_cert,
    local_sensitive_file.envoy_key,
  ]
}
