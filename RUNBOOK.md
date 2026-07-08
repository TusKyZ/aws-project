# RUNBOOK — Sentinel-AWS

Operational procedures for the deployed pipeline. Every alarm below notifies
the `sentinel-<env>-alerts` SNS topic (email and/or Slack).

First-time setup (account, state bucket, first deploy, drills):
[GETTING_STARTED.md](GETTING_STARTED.md).

## Deploy / update

```sh
python scripts/build_lambda_layer.py                       # build/layer.zip
cd terraform
terraform init -backend-config=envs/dev.backend.hcl        # once per machine
terraform plan  -var-file=envs/dev.tfvars -out=plan.out
terraform apply plan.out
```

First deploy only — push the secret values (never through Terraform):

```sh
aws secretsmanager put-secret-value --secret-id sentinel/dev/anthropic_api_key --secret-string "$ANTHROPIC_API_KEY"
aws secretsmanager put-secret-value --secret-id sentinel/dev/slack_webhook_url --secret-string "$SLACK_WEBHOOK_URL"
```

Smoke check: upload a fixture and read the audit record.

```sh
aws s3 cp tests/fixtures/dirty.csv s3://$(terraform output -raw data_bucket)/orders/dirty.csv
# wait ~1 min, then:
aws dynamodb scan --table-name $(terraform output -raw audit_table) --max-items 5
```

## CI deploys via OIDC (optional, once per account)

CI plans on PRs and applies on merge using short-lived OIDC credentials — no
AWS keys stored in GitHub. Bootstrap the federation, then wire the repo:

```sh
cd terraform/bootstrap
terraform init -backend-config=backend.hcl
terraform apply
# then run the two `gh variable set` commands from the output:
terraform output github_variable_commands
```

Finally, in GitHub → Settings → Environments → `dev`, add yourself as a
required reviewer. That approval gate is what the apply role's OIDC trust
policy keys on (`sub = repo:...:environment:dev`) — without protection rules
the workflow still works but applies without a human in the loop.

## Alarm: `*-dlq-depth` (message in the dead-letter queue)

A message failed processing 3 times. This is **never** a bad data file
(unparseable files are recorded as score-0 audit records, not retried) — it's
a genuine processing failure: IAM regression, malformed event, code bug.

1. Look at the message(s):
   ```sh
   aws sqs receive-message --queue-url <dlq-url> --max-number-of-messages 10 \
     --visibility-timeout 0
   ```
2. Correlate with logs — every log line carries the SQS `message_id`:
   ```sh
   aws logs filter-log-events --log-group-name /aws/lambda/sentinel-dev-investigator \
     --filter-pattern '"<message-id>"'
   ```
3. Fix the cause (deploy the fix if it's code).
4. Redrive the DLQ back to the ingest queue (processing is idempotent —
   already-audited files no-op):
   ```sh
   aws sqs start-message-move-task --source-arn <dlq-arn> --destination-arn <ingest-queue-arn>
   ```
5. Confirm the alarm returns to OK and the DLQ is empty.

## Alarm: `*-llm-failures` (LLM layer degraded)

Audit records are being written with `llm_status=failed`. The pipeline is
healthy (profiles + rules still run); explanation quality is degraded.

1. Find the failure reason:
   ```sh
   aws logs filter-log-events --log-group-name /aws/lambda/sentinel-dev-investigator \
     --filter-pattern '"failed"' --max-items 20
   ```
2. Triage by `failure_reason`:
   - `api_key_missing` / `authentication_error` → the secret is absent, empty,
     or revoked. Push a valid key (see key rotation below).
   - `api_error: OverloadedError` / rate limits → transient; confirm it clears.
     Persistent → lower `reserved_concurrency` or contact Anthropic.
   - `invalid_response: ...` → schema drift in the API response; check the
     anthropic SDK/model version pinning.
3. No redrive needed — degraded records are final by design. Re-upload
   specific files (new etag) if fresh LLM analysis is required.

## Anthropic API key rotation

1. Create the new key in the Anthropic console.
2. Push it:
   ```sh
   aws secretsmanager put-secret-value --secret-id sentinel/dev/anthropic_api_key \
     --secret-string "<new-key>"
   ```
3. No redeploy: warm containers hold the old key until the first 401, then
   invalidate, re-fetch, and retry automatically (see `claude_client.py`).
4. Revoke the old key in the Anthropic console.
5. Verify: upload a dirty fixture, confirm the new audit record has
   `llm_status=ok`.

## Alarm: `*-lambda-errors`

Unhandled function errors (each also retries toward the DLQ, so this usually
precedes a `dlq-depth` alarm). Read the stack trace:

```sh
aws logs filter-log-events --log-group-name /aws/lambda/sentinel-dev-investigator \
  --filter-pattern ERROR --max-items 20
```

## Budget alarm (80% actual / 100% forecast)

Check the dashboard's "LLM spend" widget first — LLM cost dominates. Levers,
in order: confirm `LLM_SKIP_ON_CLEAN=true`, lower `reserved_concurrency`,
switch `LLM_MODEL` to `claude-sonnet-5` (eval table quantifies the quality
trade-off), stop uploads.

## Teardown

```sh
cd terraform && terraform destroy -var-file=envs/dev.tfvars
```

Secrets use `recovery_window_in_days = 0`, so destroy/recreate cycles are
immediate. The state bucket is bootstrap infrastructure and survives.
