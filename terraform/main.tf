data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.env}"
  # Account id in the bucket name guarantees global uniqueness without random suffixes.
  data_bucket_name = "${local.name_prefix}-data-${data.aws_caller_identity.current.account_id}"
}

# One customer-managed key for everything the project encrypts (S3 data
# bucket, SNS alert topic). CloudWatch alarms and AWS Budgets cannot publish
# through the AWS-managed alias/aws/sns key — their service principals need
# explicit key-policy grants, which only a CMK allows.
data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "EnableIamDelegation"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid       = "AllowAlarmAndBudgetNotifications"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "budgets.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key" "project" {
  description             = "${local.name_prefix}: S3 data bucket + SNS alert topic"
  enable_key_rotation     = true
  deletion_window_in_days = 7 # minimum — fast teardown/recreate cycles
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "project" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.project.key_id
}

# Alert topic lives at the root: the Lambda publishes to it and the
# observability module points alarms at it — root ownership avoids a
# lambda <-> observability module cycle.
resource "aws_sns_topic" "alerts" {
  name              = "${local.name_prefix}-alerts"
  kms_master_key_id = aws_kms_key.project.arn
}

# CloudWatch alarms and Budgets publish via service principals — both need an
# explicit topic-policy grant (Budgets hard-fails its subscription without one).
data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AllowAlarmAndBudgetPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "budgets.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

module "s3" {
  source      = "./modules/s3"
  bucket_name = local.data_bucket_name
  kms_key_arn = aws_kms_key.project.arn
}

module "eventing" {
  source      = "./modules/eventing"
  name_prefix = local.name_prefix
  bucket_name = module.s3.bucket_name
}

module "storage" {
  source      = "./modules/storage"
  name_prefix = local.name_prefix
}

module "security" {
  source  = "./modules/security"
  project = var.project
  env     = var.env
}

module "lambda" {
  source = "./modules/lambda"

  name_prefix          = local.name_prefix
  src_dir              = "${path.root}/../src"
  layer_zip_path       = var.layer_zip_path
  memory_mb            = var.lambda_memory_mb
  reserved_concurrency = var.reserved_concurrency

  bucket_name = module.s3.bucket_name
  bucket_arn  = module.s3.bucket_arn
  kms_key_arn = aws_kms_key.project.arn
  queue_arn   = module.eventing.queue_arn
  table_name  = module.storage.table_name
  table_arn   = module.storage.table_arn
  topic_arn   = aws_sns_topic.alerts.arn

  anthropic_secret_arn = module.security.anthropic_secret_arn
  slack_secret_arn     = module.security.slack_secret_arn
  anthropic_secret_id  = module.security.anthropic_secret_name
  slack_secret_id      = module.security.slack_secret_name

  environment = {
    AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
    LLM_SKIP_ON_CLEAN    = var.llm_skip_on_clean ? "true" : "false"
    LLM_MODEL            = var.llm_model
    DATASET_PREFIX_DEPTH = tostring(var.dataset_prefix_depth)
  }
}

module "observability" {
  source = "./modules/observability"

  name_prefix        = local.name_prefix
  region             = var.region
  function_name      = module.lambda.function_name
  dlq_name           = module.eventing.dlq_name
  alert_topic_arn    = aws_sns_topic.alerts.arn
  monthly_budget_usd = var.monthly_budget_usd
  alert_email        = var.alert_email
}
