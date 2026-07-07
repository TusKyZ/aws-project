variable "name_prefix" {
  type = string
}

variable "bucket_name" {
  type = string
}

variable "suffixes" {
  description = "Only these object suffixes reach the pipeline — junk is filtered before compute."
  type        = list(string)
  default     = [".csv", ".parquet", ".json"]
}

variable "visibility_timeout_seconds" {
  description = ">= 6x the Lambda timeout (300s) per AWS guidance, else in-flight messages redeliver mid-run."
  type        = number
  default     = 1800
}

variable "max_receive_count" {
  type    = number
  default = 3
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-ingest-dlq"
  message_retention_seconds = 1209600 # 14 days to investigate + redrive
}

resource "aws_sqs_queue" "ingest" {
  name                       = "${var.name_prefix}-ingest"
  visibility_timeout_seconds = var.visibility_timeout_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_cloudwatch_event_rule" "object_created" {
  name        = "${var.name_prefix}-object-created"
  description = "S3 Object Created in the data bucket, data suffixes only."

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [var.bucket_name] }
      object = { key = [for s in var.suffixes : { suffix = s }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "to_queue" {
  rule = aws_cloudwatch_event_rule.object_created.name
  arn  = aws_sqs_queue.ingest.arn
}

resource "aws_sqs_queue_policy" "allow_eventbridge" {
  queue_url = aws_sqs_queue.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeSend"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.ingest.arn
        Condition = {
          ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.object_created.arn }
        }
      }
    ]
  })
}

output "queue_arn" {
  value = aws_sqs_queue.ingest.arn
}

output "queue_url" {
  value = aws_sqs_queue.ingest.url
}

output "dlq_name" {
  value = aws_sqs_queue.dlq.name
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}
