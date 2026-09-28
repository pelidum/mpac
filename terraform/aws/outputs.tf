output "ui_url" {
  value       = "https://${local.endpoint_host}"
  description = "MPAC web UI."
}

output "grpc_endpoint" {
  value       = "${local.endpoint_host}:50051"
  description = "MPAC gRPC server (TLS)."
}

output "alb_dns_name" {
  value       = aws_lb.main.dns_name
  description = "Load balancer hostname. Point your DNS here when using acm_certificate_arn."
}

output "certificate_mode" {
  value       = local.cert_mode
  description = "self_signed, route53 or byo."
}

output "tls_certificate_pem" {
  value       = one(tls_self_signed_cert.self_signed[*].cert_pem)
  description = "Self-signed certificate for gRPC clients to trust (self-signed mode only): terraform output -raw tls_certificate_pem > mpac.crt"
}

output "ecs_cluster_name" {
  value       = aws_ecs_cluster.main.name
  description = "ECS cluster (for aws ecs commands)."
}

output "ecs_service_name" {
  value       = aws_ecs_service.main.name
  description = "ECS service (for aws ecs commands)."
}

output "log_group_name" {
  value       = aws_cloudwatch_log_group.main.name
  description = "CloudWatch log group; streams are prefixed server/ and ui/."
}

output "db_endpoint" {
  value       = aws_db_instance.main.address
  description = "RDS hostname (reachable only from inside the VPC)."
}

output "state_backend_config" {
  value = var.create_state_bucket ? join("\n", [
    "terraform {",
    "  backend \"s3\" {",
    "    bucket       = \"${aws_s3_bucket.tf_state[0].id}\"",
    "    key          = \"${local.prefix}/terraform.tfstate\"",
    "    region       = \"${var.region}\"",
    "    encrypt      = true",
    "    use_lockfile = true",
    "  }",
    "}",
  ]) : null
  description = "Paste into backend.tf, then run `terraform init -migrate-state`."
}
