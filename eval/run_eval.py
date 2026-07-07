"""Eval runner (Phase 3): rules-only vs LLM-only vs hybrid vs hybrid-sonnet.

Scores each arm against the labeled corpus from generate_dirty_data.py.
Detection is class-level: an arm "detects" a file's injected class when its
findings map to that class (see _classes_from_* — column-based, deliberately
conservative). Anything an arm flags that isn't the file's label counts against
precision; anything flagged on a clean file feeds the false-positive rate.

Cost comes from real response `usage` tokens, never estimates. LLM calls are
injectable, so tests run the whole pipeline with a fake analyzer at zero spend;
the CLI wires real ClaudeAnalyzer instances.

Usage:
    python eval/run_eval.py --manifest eval/corpus/manifest.json --arms rules_only
    python eval/run_eval.py --manifest eval/corpus/manifest.json --out eval/results.md
"""

from __future__ import annotations

import sys
from pathlib import Path

# Script-style entrypoint: make `python eval/run_eval.py` find src/ the same
# way pytest's pythonpath config does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import argparse
import copy
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, ConfigDict

from claude_client import DEFAULT_MODEL, ClaudeAnalyzer, EnvKeyProvider, build_payload
from generate_dirty_data import ANOMALY_CLASSES, Manifest
from models import AnomalyReport, FileProfile, LlmOutcome, RuleFinding
from profiler import profile_file
from prompts import SYSTEM_PROMPT
from rules_engine import run_rules

SONNET_MODEL = "claude-sonnet-5"

# $/MTok (input, output) — used only to price real `usage` token counts.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
}

ALL_ARMS = ("rules_only", "llm_only", "hybrid", "hybrid_sonnet")

LlmFn = Callable[[FileProfile, list[RuleFinding]], LlmOutcome]


class ArmSpec(BaseModel):
    """How one arm builds its prediction."""

    model_config = ConfigDict(extra="forbid")

    name: str
    uses_llm: bool
    include_findings_in_prompt: bool = False  # hybrid prompts carry rule findings
    union_with_rules: bool = False  # hybrid prediction = rules ∪ LLM
    model: str | None = None


ARM_SPECS: dict[str, ArmSpec] = {
    "rules_only": ArmSpec(name="rules_only", uses_llm=False),
    "llm_only": ArmSpec(name="llm_only", uses_llm=True, model=DEFAULT_MODEL),
    "hybrid": ArmSpec(
        name="hybrid",
        uses_llm=True,
        include_findings_in_prompt=True,
        union_with_rules=True,
        model=DEFAULT_MODEL,
    ),
    "hybrid_sonnet": ArmSpec(
        name="hybrid_sonnet",
        uses_llm=True,
        include_findings_in_prompt=True,
        union_with_rules=True,
        model=SONNET_MODEL,
    ),
}


class ClassMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


class ArmResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arm: str
    model: str | None = None
    metrics: dict[str, ClassMetrics]
    macro_precision: float
    macro_recall: float
    macro_f1: float
    clean_fp_rate: float  # fraction of clean files with any prediction
    clean_total: int
    llm_failures: int = 0
    total_cost_usd: float = 0.0
    latency_p50_ms: float | None = None
    latency_p99_ms: float | None = None


class EvalResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    file_count: int
    arms: list[ArmResult]


class _PreparedFile(BaseModel):
    """Profile + rule findings computed once, shared by every arm."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    label: str
    profile: FileProfile
    findings: list[RuleFinding]


def _classes_from_rules(findings: list[RuleFinding]) -> set[str]:
    """Map deterministic findings onto eval anomaly classes."""
    classes: set[str] = set()
    for f in findings:
        if f.rule_id == "range_violation" and f.column == "age":
            classes.add("negative_age")
        elif f.rule_id == "null_threshold":
            classes.add("null_burst")
        elif f.rule_id == "duplicate_key":
            classes.add("duplicate_key")
        elif f.rule_id == "schema_drift":
            classes.add("schema_drift")
    return classes


_COLUMN_TO_CLASS = {
    "age": "negative_age",
    "order_date": "future_date",
    "amount_usd": "unit_mismatch",
    "email": "null_burst",
    "order_id": "duplicate_key",
}


def _classes_from_report(report: AnomalyReport) -> set[str]:
    """Map LLM anomalies onto eval classes by column (kind 'schema' -> drift)."""
    classes: set[str] = set()
    for anomaly in report.anomalies:
        if anomaly.kind == "schema":
            classes.add("schema_drift")
            continue
        mapped = _COLUMN_TO_CLASS.get(anomaly.column.lower())
        if mapped:
            classes.add(mapped)
    return classes


def prepare_files(manifest: Manifest, corpus_dir: Path) -> list[_PreparedFile]:
    """Profile every file once; run rules with the drift baseline where one exists."""
    prepared: list[_PreparedFile] = []
    for ef in manifest.files:
        profile = profile_file(corpus_dir / ef.path)
        previous = (
            profile_file(corpus_dir / ef.baseline_path) if ef.baseline_path is not None else None
        )
        findings = run_rules(profile, previous=previous)
        prepared.append(_PreparedFile(label=ef.label, profile=profile, findings=findings))
    return prepared


def _score_arm(
    spec: ArmSpec,
    prepared: list[_PreparedFile],
    llm: LlmFn | None,
    workers: int,
) -> ArmResult:
    outcomes: list[LlmOutcome | None]
    if spec.uses_llm:
        if llm is None:
            raise ValueError(f"arm {spec.name!r} needs an LLM callable")

        def call(item: _PreparedFile) -> LlmOutcome:
            findings = item.findings if spec.include_findings_in_prompt else []
            return llm(item.profile, findings)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            outcomes = list(pool.map(call, prepared))
    else:
        outcomes = [None] * len(prepared)

    metrics = {cls: ClassMetrics() for cls in ANOMALY_CLASSES}
    clean_total = clean_flagged = llm_failures = 0
    cost = 0.0
    latencies: list[float] = []

    for item, outcome in zip(prepared, outcomes, strict=True):
        predicted: set[str] = set()
        if spec.uses_llm:
            if outcome is None:  # unreachable: LLM arms fill every slot above
                raise RuntimeError(f"missing LLM outcome in arm {spec.name!r}")
            if outcome.status == "ok" and outcome.report is not None:
                predicted |= _classes_from_report(outcome.report)
            else:
                llm_failures += 1
            if outcome.usage is not None:
                rate_in, rate_out = PRICING_PER_MTOK.get(outcome.model or "", (0.0, 0.0))
                cost += (
                    outcome.usage.input_tokens * rate_in
                    + outcome.usage.output_tokens * rate_out
                ) / 1_000_000
            if outcome.latency_ms is not None:
                latencies.append(outcome.latency_ms)
        if not spec.uses_llm or spec.union_with_rules:
            predicted |= _classes_from_rules(item.findings)

        if item.label == "clean":
            clean_total += 1
            if predicted:
                clean_flagged += 1
            for cls in predicted:
                metrics[cls].fp += 1
        else:
            for cls in ANOMALY_CLASSES:
                if cls == item.label:
                    if cls in predicted:
                        metrics[cls].tp += 1
                    else:
                        metrics[cls].fn += 1
                elif cls in predicted:
                    metrics[cls].fp += 1

    per_class = list(metrics.values())
    return ArmResult(
        arm=spec.name,
        model=spec.model,
        metrics=metrics,
        macro_precision=statistics.mean(m.precision for m in per_class),
        macro_recall=statistics.mean(m.recall for m in per_class),
        macro_f1=statistics.mean(m.f1 for m in per_class),
        clean_fp_rate=(clean_flagged / clean_total) if clean_total else 0.0,
        clean_total=clean_total,
        llm_failures=llm_failures,
        total_cost_usd=round(cost, 4),
        latency_p50_ms=statistics.median(latencies) if latencies else None,
        latency_p99_ms=(
            statistics.quantiles(latencies, n=100)[98] if len(latencies) >= 2 else None
        ),
    )


def run(
    manifest: Manifest,
    corpus_dir: Path,
    arms: list[str],
    llm_factory: Callable[[str], LlmFn] | None = None,
    workers: int = 4,
) -> EvalResults:
    """Evaluate the requested arms. `llm_factory(model)` supplies the LLM callable."""
    prepared = prepare_files(manifest, corpus_dir)
    results: list[ArmResult] = []
    for name in arms:
        spec = ARM_SPECS[name]
        llm = llm_factory(spec.model) if (spec.uses_llm and llm_factory is not None) else None
        results.append(_score_arm(spec, prepared, llm, workers))
    return EvalResults(seed=manifest.seed, file_count=len(manifest.files), arms=results)


def render_results(results: EvalResults) -> str:
    """Markdown report — this is what gets committed as eval/results.md."""
    lines = [
        "# Eval results",
        "",
        f"Corpus: {results.file_count} files, seed {results.seed}.",
        "",
    ]
    for arm in results.arms:
        model = f" (`{arm.model}`)" if arm.model else ""
        lines += [f"## {arm.arm}{model}", ""]
        lines += [
            "| class | precision | recall | F1 | TP | FP | FN |",
            "|---|---|---|---|---|---|---|",
        ]
        for cls in ANOMALY_CLASSES:
            m = arm.metrics[cls]
            lines.append(
                f"| {cls} | {m.precision:.2f} | {m.recall:.2f} | {m.f1:.2f} "
                f"| {m.tp} | {m.fp} | {m.fn} |"
            )
        lines.append(
            f"| **macro** | **{arm.macro_precision:.2f}** | **{arm.macro_recall:.2f}** "
            f"| **{arm.macro_f1:.2f}** | | | |"
        )
        lines += [
            "",
            f"- Clean false-positive rate: {arm.clean_fp_rate:.1%} "
            f"of {arm.clean_total} clean files",
            f"- LLM failures: {arm.llm_failures}",
            f"- Total LLM cost: ${arm.total_cost_usd:.4f} (from real usage tokens)",
        ]
        if arm.latency_p50_ms is not None:
            p99 = f"{arm.latency_p99_ms:.0f}" if arm.latency_p99_ms is not None else "n/a"
            lines.append(f"- LLM latency: p50 {arm.latency_p50_ms:.0f}ms / p99 {p99}ms")
        lines.append("")
    return "\n".join(lines)


_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength"}
)


def _wire_schema(model_cls: type[BaseModel]) -> dict:
    """JSON schema as the API accepts it: numeric/string constraints stripped
    (unsupported server-side; the SDK does the same and validates client-side).
    """

    def strip(node: object) -> None:
        if isinstance(node, dict):
            for key in _UNSUPPORTED_SCHEMA_KEYS & node.keys():
                node.pop(key)
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)

    schema = copy.deepcopy(model_cls.model_json_schema())
    strip(schema)
    return schema


def build_batch_requests(
    prepared: list[_PreparedFile],
    *,
    model: str = DEFAULT_MODEL,
    include_findings: bool = True,
    max_tokens: int = 16_000,
) -> list[dict]:
    """Batches API request list for one LLM arm (50% discount path, Phase 6).

    Payloads are byte-identical to the synchronous client's (same build_payload,
    same system prompt); results are validated client-side with AnomalyReport.
    """
    schema = _wire_schema(AnomalyReport)
    requests = []
    for index, item in enumerate(prepared):
        findings = item.findings if include_findings else []
        requests.append(
            {
                "custom_id": f"eval-{index:05d}",
                "params": {
                    "model": model,
                    "max_tokens": max_tokens,
                    "thinking": {"type": "adaptive"},
                    "system": SYSTEM_PROMPT,
                    "messages": [
                        {"role": "user", "content": build_payload(item.profile, findings)}
                    ],
                    "output_config": {"format": {"type": "json_schema", "schema": schema}},
                },
            }
        )
    return requests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--arms", nargs="+", default=list(ALL_ARMS), choices=ALL_ARMS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None, help="write results markdown here")
    args = parser.parse_args()

    manifest = Manifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    needs_llm = any(ARM_SPECS[a].uses_llm for a in args.arms)
    if needs_llm and EnvKeyProvider().get() is None:
        sys.exit(
            "ANTHROPIC_API_KEY is not set. Run rules-only (--arms rules_only) "
            "or set the key (see .env.example)."
        )

    def llm_factory(model: str) -> LlmFn:
        analyzer = ClaudeAnalyzer(model=model)
        return analyzer.analyze

    results = run(
        manifest,
        args.manifest.parent,
        list(args.arms),
        llm_factory=llm_factory if needs_llm else None,
        workers=args.workers,
    )
    rendered = render_results(results)
    print(rendered)
    if args.out is not None:
        args.out.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"written: {args.out}")


if __name__ == "__main__":
    main()
