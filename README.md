# Sentinel-AWS — Serverless Data Quality Investigator

Event-driven data quality pipeline: new files landing in S3 are profiled with DuckDB inside Lambda, checked by a deterministic rules engine, then analyzed by Claude Opus 4.8 for logical anomalies and human-readable root-cause explanations. Results land in a DynamoDB audit log; high-severity findings alert via Slack.

> 🚧 Under construction — built in phases, see [Phases.md](Phases.md). Design: [implementation_plan.md](implementation_plan.md).

## Status

- [x] Phase 0 — Setup (repo, tooling, CI)
- [x] Phase 1 — Core pipeline (DuckDB profiler + rules engine)
- [x] Phase 2 — AI layer (Claude structured outputs) — live smoke pending API key (`pytest -m live`)
- [x] Phase 3 — Eval harness — LLM-arm smoke run pending API key (rules-only arm verified: macro F1 0.67 on the 50-file smoke corpus)
- [ ] Phase 4 — Infrastructure (Terraform) — **all code written** (modules, handler, moto tests); `terraform apply` pending an AWS account + Terraform install (see [RUNBOOK.md](RUNBOOK.md) for the deploy sequence)
- [ ] Phase 5 — Hardening & observability
- [ ] Phase 6 — Full eval & ship

## Development

```sh
conda activate aws
pip install -r requirements-dev.txt
ruff check .
pytest -m "not live"
```

Secrets: copy [.env.example](.env.example) and set `ANTHROPIC_API_KEY` in your shell.
Live tests (real API calls, a few cents): `pytest -m live -s`.

Eval harness (rules-only arm needs no key):

```sh
python eval/generate_dirty_data.py --out eval/corpus --smoke
python eval/run_eval.py --manifest eval/corpus/manifest.json --arms rules_only
# with ANTHROPIC_API_KEY set (smoke ≈ <$5):
python eval/run_eval.py --manifest eval/corpus/manifest.json --out eval/results.md
```
