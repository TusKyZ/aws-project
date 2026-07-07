# Phases — Sentinel-AWS Build Plan

Seven phases, ordered **local-first**: everything that can run on a laptop gets built and tested before any AWS resource exists. DuckDB, the rules engine, the Claude client, and the entire eval harness run locally — that's most of the project's actual logic, iterated at zero AWS cost. Infrastructure comes in only once the core is proven.

Each phase ends in a working, committed state. Don't start phase N+1 with phase N broken.

---

## Phase 0 — Setup

**Goal:** repo skeleton + tooling so every later phase has lint, tests, and CI from commit one.

- Git repo + GitHub remote, `.gitignore` (Terraform state, `.env`, `__pycache__`, eval corpora)
- Python 3.13 project (matches the Lambda runtime — dev == prod): `pyproject.toml`, dependency management, `ruff`, `pytest` configured
- Directory skeleton from implementation_plan.md (`src/`, `tests/`, `eval/`, `terraform/` — empty modules OK)
- GitHub Actions CI skeleton: lint + pytest on PR (Terraform steps come in Phase 4)

**Done when:** CI is green on a hello-world test. **Effort:** ~half a day.

---

## Phase 1 — Core Pipeline (local, no AWS, no LLM)

**Goal:** the deterministic heart — profile a file, run rules, produce typed findings. Pure TDD; everything runs against local fixture files.

- `models.py`: all Pydantic contracts first (`ColumnProfile`, `FileProfile`, `RuleFinding`, `AnomalyReport`, audit record shape) — these are the spine of every later phase
- `profiler.py`: DuckDB profiling (row count, null %, min/max, distinct, type inference, outlier sample); local-path reads now, S3 paths are a Phase 4 config change
- Unparseable-file handling: parse error → `profile_status=unparseable`, score 0
- `rules_engine.py`: null thresholds, duplicate keys, range checks, empty-file; drift check stubbed (needs DynamoDB — interface defined now, wired in Phase 5)
- Test fixtures: small clean + dirty CSV/Parquet/JSON files committed under `tests/fixtures/`

**Done when:** `pytest` green; profiling + rules produce correct typed findings on every fixture, including the corrupt one. **Effort:** 2–3 days.
**Depends on:** Phase 0.

---

## Phase 2 — AI Layer

**Goal:** Claude Opus 4.8 turns a profile + rule findings into a validated `AnomalyReport`.

- `claude_client.py`: structured outputs via `messages.parse()` + `AnomalyReport`, adaptive thinking, key from env var locally (Secrets Manager indirection arrives in Phase 4). Key handling is a pluggable provider so the Phase 4 swap is one class, not a rewrite.
- Skeleton API inputs: `.env.example` documents `ANTHROPIC_API_KEY` (+ `SLACK_WEBHOOK_URL`); a missing key degrades gracefully (`llm_status=failed`, never an exception) — same path as a Secrets Manager outage in prod
- System prompt: analyst role, untrusted-data scoping (prompt-injection hardening), built on Phase 1 contracts
- Contract tests with recorded API responses: happy path, malformed response, refusal, 401 (rotation path logic)
- **Live smoke test** (one real API call, dirty fixture file): confirm report quality is actually good before building infrastructure around it — if Opus output disappoints here, iterate the prompt now, cheaply. Marked `live`, auto-skipped until the key exists.

**Done when:** recorded-response tests green (live smoke runs once the key is provided). **Effort:** 1–2 days.
**Depends on:** Phase 1 (models, profile shape).

---

## Phase 3 — Eval Harness (local)

**Goal:** the differentiator — built early so prompt changes are measurable from now on, but the *committed* full run waits until the prompt freezes (Phase 6).

- `eval/generate_dirty_data.py`: 6 anomaly classes, seeded RNG, 50 dirty/class + 200 clean
- `eval/run_eval.py`: four arms (rules-only / LLM-only / hybrid / hybrid-sonnet on `claude-sonnet-5`), per-class P/R/F1, false-positive rate, cost from `usage` tokens, latency percentiles — the sonnet arm turns "why Opus 4.8?" into a measured answer
- `--smoke` mode (50 files, sync, <$5) — run it once now to validate the harness and get a first read on hybrid-vs-rules
- Batches API submission path written, exercised with a tiny batch

**Done when:** smoke run completes and emits a believable metrics table. **Effort:** 1–2 days.
**Depends on:** Phases 1 + 2. **No AWS infra needed.**

---

## Phase 4 — Infrastructure (Terraform + deployment)

**Goal:** the local pipeline runs in AWS, triggered by a real S3 upload.

- Remote state first: S3 backend with `use_lockfile = true` (native S3 locking, GA since Terraform 1.11 — **no DynamoDB lock table**, that pattern is deprecated)
- Terraform modules: `s3` (EventBridge notifications, block-public-access, SSE, TLS-only bucket policy), `eventing` (rule with suffix filter, SQS + DLQ + redrive, queue policy for EventBridge), `lambda` (Python 3.13 runtime, DuckDB layer, Powertools, 300s timeout, event source mapping with `ReportBatchItemFailures`, reserved concurrency), `storage` (DynamoDB, TTL on `expires_at`), `security` (secret containers only — values pushed out-of-band)
- Multi-env structure: `envs/dev.tfvars` + `envs/prod.tfvars` (only dev deploys; prod proves the layout)
- IAM least-privilege role per implementation_plan.md (no `cloudwatch:PutMetricData` — metrics ride EMF log lines)
- `lambda_function.py`: SQS batch handler wiring Phase 1–2 modules; profiler switched to `s3://` paths; Secrets Manager fetch + cache + rotation path
- Deploy sequence: skeleton echo-Lambda first (proves the event plumbing), then the real handler
- SQS visibility timeout 1800s

**Done when:** upload dirty CSV → DynamoDB audit record with anomaly report appears; upload `.png` → nothing invokes. **Effort:** 2–4 days (Terraform debugging always costs more than expected).
**Depends on:** Phases 1 + 2.

---

## Phase 5 — Hardening & Observability

**Goal:** the failure paths and operational story — the part interviewers actually probe.

- Idempotency end-to-end: conditional writes live, duplicate-event test against deployed stack
- Drift check wired: `LATEST#<dataset>` item (TTL-exempt), dataset = first key prefix
- Failure paths verified in AWS: unparseable file (no DLQ), secret-unavailable degradation (`llm_status=failed`), DLQ alarm fires on a forced poison message
- `alerting.py`: SNS → Slack webhook on high severity
- Powertools wired through the handler: structured JSON logs with SQS-message-ID correlation, custom metrics via EMF (`LlmCostUsd`, latency, `AnomalyCount`, `LlmFailureCount` alarm) + dashboard
- AWS Budgets alarm ($20/month)
- `RUNBOOK.md`: DLQ alarm → inspect + redrive (`start-message-move-task`), key rotation, `LlmFailureCount` response — then actually execute each procedure once against the deployed stack
- CI completed: OIDC role, `terraform plan` on PR, apply-on-merge with approval gate
- Evidence artifacts: dashboard screenshot + firing-alarm screenshot saved for the README

**Done when:** every failure mode from MyNotes Q&A has been *demonstrated*, not just designed — each one is now a true interview story. **Effort:** 2–3 days.
**Depends on:** Phase 4.

---

## Phase 6 — Eval Full Run & Ship

**Goal:** freeze, measure, package.

- Prompt freeze — no prompt edits after this point without re-running eval
- Full eval via Batches API (~$15–30, <1 hr); commit `eval/results.md`
- README: architecture diagram, eval table, cost-per-file, monthly infra cost table at 1K files/day, p99 latency, demo GIF (upload → Slack alert), dashboard screenshot
- MyNotes.md sync pass: every Q&A answer matches what was actually built and measured; fill in real numbers
- Resume bullets drafted from measured numbers

**Done when:** a stranger can clone the repo, read the README, and see numbers + a working demo. **Effort:** 1–2 days.
**Depends on:** Phases 3 + 5.

---

## Sequence & Effort Summary

| Phase | Name | Effort | Needs AWS? | Needs API key? |
|---|---|---|---|---|
| 0 | Setup | 0.5 d | No | No |
| 1 | Core Pipeline | 2–3 d | No | No |
| 2 | AI Layer | 1–2 d | No | Yes (1 live call) |
| 3 | Eval Harness | 1–2 d | No | Yes (smoke, <$5) |
| 4 | Infrastructure | 2–4 d | Yes | Yes |
| 5 | Hardening & Observability | 2–3 d | Yes | Yes |
| 6 | Eval Full Run & Ship | 1–2 d | Yes | Yes (~$15–30) |

**Total: roughly 10–17 working days** (2–3 weeks part-time). Phases 1→2→3 are strictly local — more than half the project ships before the first `terraform apply`.

**Rules of the road:**
- TDD inside every phase: test first, then code (the plan's Verification section maps tests to phases).
- One PR per phase minimum; CI green before merge.
- If Phase 2's live smoke shows weak Opus output, stop and iterate the prompt there — it's the cheapest place in the whole project to fix quality.
- Track LLM spend from Phase 2 onward; the budget alarm doesn't exist until Phase 5, so until then the `--smoke` discipline is the cost control.
