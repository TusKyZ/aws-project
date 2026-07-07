variable "project" {
  type = string
}

variable "env" {
  type = string
}

# Containers only — the values are pushed out-of-band with
# `aws secretsmanager put-secret-value` so no secret material ever enters
# Terraform state or plan output (see MyNotes.md).

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name        = "${var.project}/${var.env}/anthropic_api_key"
  description = "Anthropic API key for the investigator Lambda. Value set out-of-band."

  # Portfolio project: allow immediate delete/recreate cycles.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "slack_webhook_url" {
  name        = "${var.project}/${var.env}/slack_webhook_url"
  description = "Slack incoming-webhook URL for high-severity alerts. Value set out-of-band."

  recovery_window_in_days = 0
}

output "anthropic_secret_arn" {
  value = aws_secretsmanager_secret.anthropic_api_key.arn
}

output "anthropic_secret_name" {
  value = aws_secretsmanager_secret.anthropic_api_key.name
}

output "slack_secret_arn" {
  value = aws_secretsmanager_secret.slack_webhook_url.arn
}

output "slack_secret_name" {
  value = aws_secretsmanager_secret.slack_webhook_url.name
}
