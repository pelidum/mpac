# TLS certificate for the ALB
#
# An ALB can't serve HTTPS without a certificate, and unlike Cloud Run there is
# no free managed hostname with one. Three modes:
#
#   self_signed (default) - nothing to prepare, same as on_prem. Generated in
#                           pure Terraform for the ALB's DNS name and imported
#                           into ACM. Browsers warn; gRPC clients must trust
#                           the `tls_certificate_pem` output.
#   route53               - var.domain_name + var.route53_zone_id. Publicly
#                           trusted ACM certificate, DNS-validated, plus an
#                           alias record, all automatic.
#   byo                   - var.acm_certificate_arn. Your own ACM certificate;
#                           point your DNS at the `alb_dns_name` output.

locals {
  cert_mode = (
    var.acm_certificate_arn != "" ? "byo" :
    var.domain_name != "" ? "route53" :
    "self_signed"
  )
  trusted_cert = local.cert_mode != "self_signed"

  endpoint_host = var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name

  certificate_arn = {
    byo         = var.acm_certificate_arn
    route53     = one(aws_acm_certificate_validation.domain[*].certificate_arn)
    self_signed = one(aws_acm_certificate.self_signed[*].arn)
  }[local.cert_mode]
}

# Self-signed (the on_prem pattern)
#
# The private key is kept in state (ACM import needs it), unlike every other
# secret here. It only protects a self-signed certificate; use a real domain
# for anything beyond evaluation.
resource "tls_private_key" "self_signed" {
  count     = local.cert_mode == "self_signed" ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "self_signed" {
  count           = local.cert_mode == "self_signed" ? 1 : 0
  private_key_pem = tls_private_key.self_signed[0].private_key_pem

  subject {
    # The CN is capped at 64 characters, which ALB DNS names can exceed, so the
    # hostname goes in the SAN only (all modern clients check the SAN).
    common_name  = local.prefix
    organization = local.prefix
  }

  dns_names = [aws_lb.main.dns_name]

  validity_period_hours = var.tls_validity_days * 24

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

resource "aws_acm_certificate" "self_signed" {
  count            = local.cert_mode == "self_signed" ? 1 : 0
  private_key      = tls_private_key.self_signed[0].private_key_pem
  certificate_body = tls_self_signed_cert.self_signed[0].cert_pem

  tags = { Name = "${local.prefix}-self-signed" }

  lifecycle {
    create_before_destroy = true
  }
}

# Route 53 + ACM
resource "aws_acm_certificate" "domain" {
  count             = local.cert_mode == "route53" ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"

  tags = { Name = "${local.prefix}-${var.domain_name}" }

  lifecycle {
    create_before_destroy = true
  }
}

# Keyed by the (plan-time known) domain name rather than by the validation
# options themselves, so for_each never depends on values AWS returns later.
resource "aws_route53_record" "cert_validation" {
  for_each = local.cert_mode == "route53" ? toset([var.domain_name]) : toset([])

  zone_id         = var.route53_zone_id
  name            = local.cert_validation[each.key].resource_record_name
  type            = local.cert_validation[each.key].resource_record_type
  records         = [local.cert_validation[each.key].resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

locals {
  cert_validation = {
    for dvo in flatten(aws_acm_certificate.domain[*].domain_validation_options) : dvo.domain_name => dvo
  }
}

resource "aws_acm_certificate_validation" "domain" {
  count                   = local.cert_mode == "route53" ? 1 : 0
  certificate_arn         = aws_acm_certificate.domain[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "endpoint" {
  count   = local.cert_mode == "route53" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
