data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.env}"
  # Account id in the bucket name guarantees global uniqueness without random suffixes.
  data_bucket_name = "${local.name_prefix}-data-${data.aws_caller_identity.current.account_id}"
}

# Alert topic lives at the root: the Lambda publishes to it and the
# observability module points alarms at it — root ownership avoids a
# lambda <-> observability module cycle.
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
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
  source      = "./modules/security"
  project     = var.project
  env         = var.env
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
