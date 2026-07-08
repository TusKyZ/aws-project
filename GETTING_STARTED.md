# Getting Started — from zero to a running pipeline

Ordered checklist for standing this project up from nothing. Do the stages in
order; each one says what it costs and how to verify it worked. Day-2
operations (alarms, redrives, rotation) live in [RUNBOOK.md](RUNBOOK.md).

> **Cost expectations:** ≈ $11/month infrastructure while deployed (see README
> cost table), one-time ~$20–35 for the full eval, a few cents for smoke
> tests. `terraform destroy` between demo sessions drops idle cost to ~$0
> (the budget alarm at $20/month is the backstop).

## Stage 0 — One-time installs

- [ ] Python env (skip if `conda activate aws` already works):
  ```sh
  conda create -n aws python=3.13
  conda activate aws
  pip install -r requirements-dev.txt
  ```
- [ ] Verify the local loop is green before touching the cloud:
  ```sh
  ruff check .
  pytest -m "not live"     # expect: 65 passed, 1 skipped
  ```
- [ ] AWS CLI v2: `winget install Amazon.AWSCLI`
- [ ] Terraform ≥ 1.11: `winget install Hashicorp.Terraform`
- [ ] Optional (CI wiring in Stage 6): GitHub CLI, `winget install GitHub.cli`

## Stage 1 — Anthropic API key (no AWS needed — do this first)

The cheapest place to discover a quality problem is before any infrastructure
exists (see "Rules of the road" in [Phases.md](Phases.md)).

- [ ] Create a key at console.anthropic.com → API keys
- [ ] Set it for your shell (pick one; see [.env.example](.env.example)):
  ```powershell
  # PowerShell (current session)
  $env:ANTHROPIC_API_KEY = "sk-ant-..."
  # or bake it into the conda env (persists):
  conda env config vars set -n aws ANTHROPIC_API_KEY=sk-ant-...
  conda activate aws
  ```
- [ ] Live smoke — one real Opus call against a dirty fixture (a few cents):
  ```sh
  pytest -m live -s
  ```
  **Read the printed report.** If the explanations are weak, iterate
  `src/prompts.py` *now* and re-run — after this gate the prompt heads toward
  freezing (Phase 6).
- [ ] Smoke eval with LLM arms (~<$5, 50 files):
  ```sh
  python eval/generate_dirty_data.py --out eval/corpus --smoke
  python eval/run_eval.py --manifest eval/corpus/manifest.json
  ```

## Stage 2 — AWS account

- [ ] Create the account, put MFA on the root user, then stop using root.
- [ ] Create an admin identity for yourself (IAM Identity Center is the
      recommended path; a plain IAM user with `AdministratorAccess` and an
      access key is the acceptable solo-dev shortcut).
- [ ] Configure and verify:
  ```sh
  aws configure          # region: us-east-1, output: json
  aws sts get-caller-identity
  ```

## Stage 3 — Terraform state bucket (once per account)

The only resource created outside Terraform (chicken-and-egg — state needs
somewhere to live). Pick a globally unique name like `<yourname>-sentinel-tfstate`:

```sh
aws s3api create-bucket --bucket <yourname>-sentinel-tfstate
aws s3api put-bucket-versioning --bucket <yourname>-sentinel-tfstate \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <yourname>-sentinel-tfstate \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket <yourname>-sentinel-tfstate \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

(`create-bucket` as written assumes us-east-1; any other region needs
`--create-bucket-configuration LocationConstraint=<region>`.)

- [ ] Replace `CHANGEME-sentinel-tfstate` with your bucket name in all three:
  - `terraform/envs/dev.backend.hcl`
  - `terraform/envs/prod.backend.hcl`
  - `terraform/bootstrap/backend.hcl`

## Stage 4 — Deploy dev

```sh
python scripts/build_lambda_layer.py            # build/layer.zip (~27 MB)
cd terraform
terraform init -backend-config=envs/dev.backend.hcl
terraform plan -var-file=envs/dev.tfvars -out=plan.out
terraform apply plan.out
```

- [ ] Optional first: set `alert_email` in `terraform/envs/dev.tfvars` to get
      alarm emails (AWS sends a confirmation link — click it).
- [ ] Push the secret values (never through Terraform — they'd land in state):
  ```sh
  terraform output secret_push_commands   # prints the two exact commands
  aws secretsmanager put-secret-value --secret-id sentinel/dev/anthropic_api_key --secret-string "$env:ANTHROPIC_API_KEY"
  aws secretsmanager put-secret-value --secret-id sentinel/dev/slack_webhook_url --secret-string "<webhook-or-empty>"
  ```
- [ ] Smoke: upload a dirty fixture, expect an audit record within ~1 min:
  ```sh
  aws s3 cp ../tests/fixtures/dirty.csv s3://$(terraform output -raw data_bucket)/orders/dirty.csv
  aws dynamodb scan --table-name $(terraform output -raw audit_table) --max-items 5
  ```
  Also check: `aws s3 cp` a `.png` → nothing invokes (suffix filter works);
  the CloudWatch dashboard (`terraform output -raw dashboard_name`) shows the
  invocation.

## Stage 5 — Prove the failure paths (Phase 5 drills)

Each drill turns a designed behavior into a demonstrated one. Take
screenshots of the firing alarms + dashboard for the README.

- [ ] **Duplicate delivery**: upload the same file twice → one audit record,
      `DuplicateSkipped` metric ticks, LLM billed once.
- [ ] **Corrupt file**: upload a garbage `.csv` → score-0 audit record with
      `profile_status=unparseable`; DLQ stays empty.
- [ ] **LLM outage**: overwrite the Anthropic secret with `"invalid"` → upload
      a dirty file → record has `llm_status=failed` + deterministic fallback
      score; `*-llm-failures` alarm fires. Restore the real key → next upload
      self-heals (no redeploy). This doubles as the key-rotation drill.
- [ ] **Poison message**: `aws sqs send-message` raw garbage to the ingest
      queue → three failed receives → lands in DLQ → `*-dlq-depth` alarm →
      walk the RUNBOOK redrive procedure.

## Stage 6 — Optional: CI does the deploys

```sh
cd terraform/bootstrap
terraform init -backend-config=backend.hcl
terraform apply
terraform output github_variable_commands    # run the two gh commands it prints
```

- [ ] GitHub → Settings → Environments → `dev` → add yourself as required
      reviewer. From now on: PRs get a read-only plan, merges to main wait for
      your approval, then apply — no AWS keys stored in GitHub.

## Stage 7 — Full eval & ship (Phase 6)

- [ ] Freeze `src/prompts.py` (any later edit = re-run the eval).
- [ ] Full 4-arm eval over the 500-file corpus (~$20–35, Batches API):
  ```sh
  python eval/run_eval.py --manifest eval/corpus/manifest.json --out eval/results.md
  ```
- [ ] Commit `eval/results.md`; replace the README cost-table LLM estimate and
      MyNotes TBDs with measured numbers; record p99 latency from the
      dashboard; capture the demo GIF (upload → Slack alert).
- [ ] Idle? `terraform destroy -var-file=envs/dev.tfvars` — state bucket and
      bootstrap survive, redeploy is Stage 4 only.
