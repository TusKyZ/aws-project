"""Synthetic labeled eval corpus (Phase 3).

Generates CSV files with known-injected anomalies plus clean controls, and a
manifest.json mapping every file to its ground-truth label. Fully deterministic
from the seed (fixed date anchor, seeded RNG) — the corpus is regenerated, never
committed.

Anomaly classes and who should catch them:

  negative_age    rules (age range rule)         + LLM
  future_date     LLM only (no rule exists)
  unit_mismatch   LLM only (amounts x100 — no rule exists)
  null_burst      rules (null threshold)         + LLM
  duplicate_key   rules (duplicate key)          + LLM
  schema_drift    rules (needs baseline profile) — LLM can't see the baseline

Usage:
    python eval/generate_dirty_data.py --out eval/corpus            # full (500 files)
    python eval/generate_dirty_data.py --out eval/corpus --smoke    # 50 files
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ANOMALY_CLASSES: tuple[str, ...] = (
    "negative_age",
    "future_date",
    "unit_mismatch",
    "null_burst",
    "duplicate_key",
    "schema_drift",
)

Label = Literal[
    "clean",
    "negative_age",
    "future_date",
    "unit_mismatch",
    "null_burst",
    "duplicate_key",
    "schema_drift",
]

# Fixed anchor instead of date.today() so the same seed always yields
# byte-identical files, regardless of when the corpus is regenerated.
ANCHOR_DATE = dt.date(2026, 1, 1)

HEADER = ["order_id", "age", "email", "amount_usd", "order_date", "status"]
_STATUSES = ("pending", "shipped", "delivered", "returned")


class EvalFile(BaseModel):
    """One corpus file and its ground truth."""

    model_config = ConfigDict(extra="forbid")

    path: str  # relative to the corpus root
    label: Label
    baseline_path: str | None = None  # schema_drift only: the pre-drift twin


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    rows_per_file: int
    files: list[EvalFile]


class GenerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_dir: Path
    seed: int = 42
    dirty_per_class: int = 50
    clean_count: int = 200
    rows_per_file: int = Field(default=1000, ge=10)


def _base_rows(rng: random.Random, n: int) -> list[dict]:
    """A clean file: unique keys, sane ages, past dates, dollar amounts."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "order_id": 10_000 + i,
                "age": rng.randint(18, 90),
                # ~2% missing emails: realistic, safely below the 50% threshold.
                "email": None if rng.random() < 0.02 else f"user{10_000 + i}@example.com",
                "amount_usd": round(rng.uniform(5.0, 500.0), 2),
                "order_date": (ANCHOR_DATE - dt.timedelta(days=rng.randint(1, 730))).isoformat(),
                "status": rng.choice(_STATUSES),
            }
        )
    return rows


def _corrupt(label: str, rows: list[dict], rng: random.Random) -> None:
    """Inject exactly one anomaly class, in place."""
    n = len(rows)
    if label == "negative_age":
        for i in rng.sample(range(n), max(1, n // 20)):  # ~5%
            rows[i]["age"] = -abs(rows[i]["age"])
    elif label == "future_date":
        for i in rng.sample(range(n), max(1, n // 20)):  # ~5%
            future = ANCHOR_DATE + dt.timedelta(days=rng.randint(2 * 365, 20 * 365))
            rows[i]["order_date"] = future.isoformat()
    elif label == "unit_mismatch":
        for i in rng.sample(range(n), max(1, int(n * 0.4))):  # ~40% in cents
            rows[i]["amount_usd"] = round(rows[i]["amount_usd"] * 100, 2)
    elif label == "null_burst":
        for i in rng.sample(range(n), max(1, int(n * 0.7))):  # ~70% null
            rows[i]["email"] = None
    elif label == "duplicate_key":
        for i in rng.sample(range(1, n), max(1, n // 10)):  # ~10% duplicated keys
            rows[i]["order_id"] = rows[rng.randrange(0, i)]["order_id"]
    elif label == "schema_drift":
        # Retype amount_usd: numbers become "USD 12.34" strings, so DuckDB
        # infers VARCHAR where the baseline had DOUBLE.
        for row in rows:
            row["amount_usd"] = f"USD {row['amount_usd']:.2f}"
    else:  # pragma: no cover - guarded by the Label literal
        raise ValueError(f"unknown label: {label}")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def generate(config: GenerationConfig) -> Manifest:
    """Generate the corpus + manifest.json under config.out_dir."""
    rng = random.Random(config.seed)
    out = config.out_dir
    files: list[EvalFile] = []

    for i in range(config.clean_count):
        rel = f"clean/clean_{i:04d}.csv"
        _write_csv(out / rel, _base_rows(rng, config.rows_per_file))
        files.append(EvalFile(path=rel, label="clean"))

    for label in ANOMALY_CLASSES:
        for i in range(config.dirty_per_class):
            rows = _base_rows(rng, config.rows_per_file)
            baseline_rel: str | None = None
            if label == "schema_drift":
                # The pre-drift twin is the drift baseline (prod: LATEST#<dataset>).
                baseline_rel = f"{label}/baseline_{i:04d}.csv"
                _write_csv(out / baseline_rel, rows)
                rows = [dict(row) for row in rows]
            _corrupt(label, rows, rng)
            rel = f"{label}/{label}_{i:04d}.csv"
            _write_csv(out / rel, rows)
            files.append(EvalFile(path=rel, label=label, baseline_path=baseline_rel))  # type: ignore[arg-type]

    manifest = Manifest(seed=config.seed, rows_per_file=config.rows_per_file, files=files)
    (out / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="corpus output directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dirty-per-class", type=int, default=50)
    parser.add_argument("--clean", type=int, default=200)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument(
        "--smoke", action="store_true", help="tiny corpus: 5 dirty/class + 20 clean, 200 rows"
    )
    args = parser.parse_args()

    config = GenerationConfig(
        out_dir=args.out,
        seed=args.seed,
        dirty_per_class=5 if args.smoke else args.dirty_per_class,
        clean_count=20 if args.smoke else args.clean,
        rows_per_file=200 if args.smoke else args.rows,
    )
    manifest = generate(config)
    dirty = sum(1 for f in manifest.files if f.label != "clean")
    clean = len(manifest.files) - dirty
    print(f"corpus: {len(manifest.files)} files ({dirty} dirty, {clean} clean) -> {config.out_dir}")


if __name__ == "__main__":
    main()
