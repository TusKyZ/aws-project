# Eval results

Corpus: 500 files, seed 42.

## rules_only

| class | precision | recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| negative_age | 1.00 | 1.00 | 1.00 | 50 | 0 | 0 |
| future_date | 0.00 | 0.00 | 0.00 | 0 | 0 | 50 |
| unit_mismatch | 0.00 | 0.00 | 0.00 | 0 | 0 | 50 |
| null_burst | 1.00 | 1.00 | 1.00 | 50 | 0 | 0 |
| duplicate_key | 1.00 | 1.00 | 1.00 | 50 | 0 | 0 |
| schema_drift | 1.00 | 1.00 | 1.00 | 50 | 0 | 0 |
| **macro** | **0.67** | **0.67** | **0.67** | | | |

- Clean false-positive rate: 0.0% of 200 clean files
- LLM failures: 0
- Total LLM cost: $0.0000 (from real usage tokens)
