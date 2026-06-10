# Sentinel-AWS — Serverless Data Quality Investigator

Event-driven data quality pipeline: new files landing in S3 are profiled with DuckDB inside Lambda, checked by a deterministic rules engine, then analyzed by Claude Opus 4.8 for logical anomalies and human-readable root-cause explanations. Results land in a DynamoDB audit log; high-severity findings alert via Slack.

> 🚧 Under construction — built in phases, see [Phases.md](Phases.md). Design: [implementation_plan.md](implementation_plan.md).

## Status

- [x] Phase 0 — Setup (repo, tooling, CI)
- [ ] Phase 1 — Core pipeline (DuckDB profiler + rules engine)
- [ ] Phase 2 — AI layer (Claude structured outputs)
- [ ] Phase 3 — Eval harness
- [ ] Phase 4 — Infrastructure (Terraform)
- [ ] Phase 5 — Hardening & observability
- [ ] Phase 6 — Full eval & ship

## Development

```sh
conda activate aws
pip install -r requirements-dev.txt
ruff check .
pytest -m "not live"
```
