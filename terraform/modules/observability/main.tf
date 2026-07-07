variable "name_prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "function_name" {
  type = string
}

variable "dlq_name" {
  type = string
}

variable "alert_topic_arn" {
  type = string
}

variable "monthly_budget_usd" {
  type = number
}

variable "alert_email" {
  type    = string
  default = ""
}

# --- Alarms (all notify the shared alert topic) ---

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.name_prefix}-dlq-depth"
  alarm_description   = "Messages in the DLQ — a file the pipeline could not process. See RUNBOOK.md."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.name_prefix}-lambda-errors"
  alarm_description   = "Investigator function errored (post-retry failures land in the DLQ)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = var.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
}

# LlmFailureCount arrives via EMF (Powertools dimension: service=sentinel).
# Degradation is loud, never silent: rules still run, but a human finds out.
resource "aws_cloudwatch_metric_alarm" "llm_failures" {
  alarm_name          = "${var.name_prefix}-llm-failures"
  alarm_description   = "LLM layer degraded (llm_status=failed audit records being written)."
  namespace           = "Sentinel"
  metric_name         = "LlmFailureCount"
  dimensions          = { service = "sentinel" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
}

# --- Dashboard ---

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.name_prefix}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Data quality score / anomalies"
          region = var.region
          stat   = "Average"
          period = 300
          metrics = [
            ["Sentinel", "DataQualityScore", "service", "sentinel"],
            ["Sentinel", "AnomalyCount", "service", "sentinel", { stat = "Sum" }],
            ["Sentinel", "RuleFindingCount", "service", "sentinel", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Latency (p50 / p99)"
          region = var.region
          period = 300
          metrics = [
            ["Sentinel", "ProfileDurationMs", "service", "sentinel", { stat = "p50" }],
            ["Sentinel", "ProfileDurationMs", "service", "sentinel", { stat = "p99" }],
            ["Sentinel", "LlmLatencyMs", "service", "sentinel", { stat = "p50" }],
            ["Sentinel", "LlmLatencyMs", "service", "sentinel", { stat = "p99" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "LLM spend (USD) / failures / duplicates"
          region = var.region
          stat   = "Sum"
          period = 3600
          metrics = [
            ["Sentinel", "LlmCostUsd", "service", "sentinel"],
            ["Sentinel", "LlmFailureCount", "service", "sentinel"],
            ["Sentinel", "DuplicateSkipped", "service", "sentinel"],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Pipeline health"
          region = var.region
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", var.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", var.function_name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_name, { stat = "Maximum" }],
          ]
        }
      },
    ]
  })
}

# --- Budget guardrail ---

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [var.alert_topic_arn]
    subscriber_email_addresses = var.alert_email == "" ? [] : [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [var.alert_topic_arn]
    subscriber_email_addresses = var.alert_email == "" ? [] : [var.alert_email]
  }
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.main.dashboard_name
}
