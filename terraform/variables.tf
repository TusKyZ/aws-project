variable "project" {
  description = "Project slug used in resource names."
  type        = string
  default     = "sentinel"
}

variable "env" {
  description = "Environment name (dev, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "audit_retention_days" {
  description = "DynamoDB TTL for audit records (drift baselines never expire)."
  type        = number
  default     = 90
}

variable "reserved_concurrency" {
  description = "Lambda reserved concurrency — caps blast radius and LLM spend on an upload flood."
  type        = number
  default     = 5
}

variable "lambda_memory_mb" {
  description = "Lambda memory (DuckDB streams; 1024 is comfortable for <1GB files)."
  type        = number
  default     = 1024
}

variable "llm_skip_on_clean" {
  description = "Skip the LLM call for files that pass every deterministic rule."
  type        = bool
  default     = true
}

variable "llm_model" {
  description = "Anthropic model id used by the analyzer."
  type        = string
  default     = "claude-opus-4-8"
}

variable "dataset_prefix_depth" {
  description = "How many leading key segments identify a dataset (drift baseline granularity)."
  type        = number
  default     = 1
}

variable "alert_email" {
  description = "Optional email subscription for the alert topic (empty = none; Slack webhook is configured as a secret out-of-band)."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "AWS Budgets monthly cost guardrail."
  type        = number
  default     = 20
}

variable "layer_zip_path" {
  description = "Path to the DuckDB/deps Lambda layer zip (build with scripts/build_lambda_layer.py)."
  type        = string
  default     = "../build/layer.zip"
}
