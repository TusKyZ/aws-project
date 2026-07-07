# Dev environment — the one that actually deploys.
env    = "dev"
region = "us-east-1"

audit_retention_days = 90
reserved_concurrency = 5
lambda_memory_mb     = 1024
llm_skip_on_clean    = true
llm_model            = "claude-opus-4-8"
monthly_budget_usd   = 20

# Set to receive alert emails (confirm the subscription email AWS sends):
alert_email = ""
