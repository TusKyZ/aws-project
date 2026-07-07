# Prod environment — exists to prove the multi-env structure.
# Never auto-applied; CI only ever applies dev (see implementation_plan.md).
env    = "prod"
region = "us-east-1"

audit_retention_days = 365
reserved_concurrency = 10
lambda_memory_mb     = 2048
llm_skip_on_clean    = true
llm_model            = "claude-opus-4-8"
monthly_budget_usd   = 100

alert_email = ""
