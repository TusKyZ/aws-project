output "data_bucket" {
  description = "Drop files here to trigger the pipeline."
  value       = module.s3.bucket_name
}

output "audit_table" {
  value = module.storage.table_name
}

output "ingest_queue_url" {
  value = module.eventing.queue_url
}

output "dlq_name" {
  value = module.eventing.dlq_name
}

output "function_name" {
  value = module.lambda.function_name
}

output "alert_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "dashboard_name" {
  value = module.observability.dashboard_name
}

output "secret_push_commands" {
  description = "Secrets are containers only — push the values out-of-band (never via Terraform)."
  value       = <<-EOT
    aws secretsmanager put-secret-value --secret-id ${module.security.anthropic_secret_name} --secret-string "$ANTHROPIC_API_KEY"
    aws secretsmanager put-secret-value --secret-id ${module.security.slack_secret_name} --secret-string "$SLACK_WEBHOOK_URL"
  EOT
}
