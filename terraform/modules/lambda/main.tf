variable "name_prefix" {
  type = string
}

variable "src_dir" {
  type = string
}

variable "layer_zip_path" {
  type = string
}

variable "memory_mb" {
  type = number
}

variable "reserved_concurrency" {
  type = number
}

variable "bucket_name" {
  type = string
}

variable "bucket_arn" {
  type = string
}

variable "queue_arn" {
  type = string
}

variable "table_name" {
  type = string
}

variable "table_arn" {
  type = string
}

variable "topic_arn" {
  type = string
}

variable "anthropic_secret_arn" {
  type = string
}

variable "slack_secret_arn" {
  type = string
}

variable "anthropic_secret_id" {
  type = string
}

variable "slack_secret_id" {
  type = string
}

variable "environment" {
  description = "Extra environment variables (retention, skip flag, model, prefix depth)."
  type        = map(string)
  default     = {}
}

locals {
  function_name = "${var.name_prefix}-investigator"
}

data "archive_file" "function" {
  type        = "zip"
  source_dir  = var.src_dir
  output_path = "${path.root}/../build/function.zip"
  excludes    = ["**/__pycache__/**"]
}

# DuckDB + anthropic + pydantic + powertools. Built by
# scripts/build_lambda_layer.py (manylinux wheels for the Lambda platform).
resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${var.name_prefix}-deps"
  filename            = var.layer_zip_path
  source_code_hash    = filebase64sha256(var.layer_zip_path)
  compatible_runtimes = ["python3.13"]
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# Least privilege, per implementation_plan.md. Note: no cloudwatch:PutMetricData —
# custom metrics ride EMF log lines through the logs permissions below.
resource "aws_iam_role_policy" "lambda" {
  name = "${local.function_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDataObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.bucket_arn}/*"
      },
      {
        Sid      = "ListDataBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.bucket_arn
      },
      {
        Sid      = "ConsumeIngestQueue"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = var.queue_arn
      },
      {
        Sid      = "ReadSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.anthropic_secret_arn, var.slack_secret_arn]
      },
      {
        Sid      = "AuditTable"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = var.table_arn
      },
      {
        Sid      = "PublishAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.topic_arn
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.function_name}*"
      }
    ]
  })
}

resource "aws_lambda_function" "investigator" {
  function_name = local.function_name
  role          = aws_iam_role.lambda.arn

  runtime = "python3.13"
  handler = "lambda_function.lambda_handler"

  filename         = data.archive_file.function.output_path
  source_code_hash = data.archive_file.function.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]

  timeout                        = 300 # DuckDB profile + one Opus call with thinking
  memory_size                    = var.memory_mb
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = merge(
      {
        TABLE_NAME              = var.table_name
        ANTHROPIC_SECRET_ID     = var.anthropic_secret_id
        SLACK_SECRET_ID         = var.slack_secret_id
        ALERT_TOPIC_ARN         = var.topic_arn
        POWERTOOLS_SERVICE_NAME = "sentinel"
        LOG_LEVEL               = "INFO"
      },
      var.environment,
    )
  }
}

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn = var.queue_arn
  function_name    = aws_lambda_function.investigator.arn
  batch_size       = 5

  # One bad file must not poison the batch.
  function_response_types = ["ReportBatchItemFailures"]
}

output "function_name" {
  value = aws_lambda_function.investigator.function_name
}

output "function_arn" {
  value = aws_lambda_function.investigator.arn
}
