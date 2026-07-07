"""Phase 3 tests: corpus generator + eval runner, zero API spend.

The runner is exercised end to end (real profiling, real rules engine) with an
injected fake LLM whose answers are keyed off the ground-truth label — so the
scoring math, arm definitions, and cost accounting are all verified without a
network call. Real LLM arms only differ by the injected callable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from generate_dirty_data import (
    ANCHOR_DATE,
    ANOMALY_CLASSES,
    GenerationConfig,
    Manifest,
    generate,
)
from models import Anomaly, AnomalyReport, FileProfile, LlmOutcome, LlmUsage, RuleFinding
from profiler import profile_file
from rules_engine import run_rules
from run_eval import (
    ALL_ARMS,
    build_batch_requests,
    prepare_files,
    render_results,
    run,
)

_TINY = dict(seed=7, dirty_per_class=2, clean_count=4, rows_per_file=60)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Manifest, Path]:
    out = tmp_path_factory.mktemp("corpus")
    manifest = generate(GenerationConfig(out_dir=out, **_TINY))
    return manifest, out


# --- generator ---


def test_corpus_counts_and_manifest(corpus) -> None:
    manifest, out = corpus
    assert len(manifest.files) == 4 + 2 * len(ANOMALY_CLASSES)
    labels = {f.label for f in manifest.files}
    assert labels == {"clean", *ANOMALY_CLASSES}
    assert (out / "manifest.json").exists()
    for ef in manifest.files:
        assert (out / ef.path).exists()
        assert (ef.baseline_path is not None) == (ef.label == "schema_drift")


def test_generation_is_deterministic(tmp_path: Path) -> None:
    a = generate(GenerationConfig(out_dir=tmp_path / "a", **_TINY))
    b = generate(GenerationConfig(out_dir=tmp_path / "b", **_TINY))
    assert a == b
    sample = a.files[-1].path  # a dirty file
    hash_a = hashlib.sha256((tmp_path / "a" / sample).read_bytes()).hexdigest()
    hash_b = hashlib.sha256((tmp_path / "b" / sample).read_bytes()).hexdigest()
    assert hash_a == hash_b


def _first(manifest: Manifest, label: str):
    return next(f for f in manifest.files if f.label == label)


def test_clean_files_trigger_no_rules(corpus) -> None:
    manifest, out = corpus
    profile = profile_file(out / _first(manifest, "clean").path)
    assert run_rules(profile) == []


def test_rules_detectable_corruptions_fire(corpus) -> None:
    manifest, out = corpus
    cases = {
        "negative_age": "range_violation",
        "null_burst": "null_threshold",
        "duplicate_key": "duplicate_key",
    }
    for label, expected_rule in cases.items():
        profile = profile_file(out / _first(manifest, label).path)
        rule_ids = {f.rule_id for f in run_rules(profile)}
        assert expected_rule in rule_ids, f"{label}: expected {expected_rule}, got {rule_ids}"


def test_llm_only_corruptions_visible_in_profile_not_rules(corpus) -> None:
    manifest, out = corpus
    future = profile_file(out / _first(manifest, "future_date").path)
    max_year = int((future.column("order_date").max_value or "0")[:4])
    assert max_year >= ANCHOR_DATE.year + 2  # corruption present in the stats
    assert run_rules(future) == []  # but invisible to the rules engine

    unit = profile_file(out / _first(manifest, "unit_mismatch").path)
    assert (unit.column("amount_usd").numeric_max or 0) > 1_000
    assert run_rules(unit) == []


def test_schema_drift_needs_baseline(corpus) -> None:
    manifest, out = corpus
    ef = _first(manifest, "schema_drift")
    drifted = profile_file(out / ef.path)
    baseline = profile_file(out / ef.baseline_path)
    assert run_rules(drifted) == []  # invisible without the baseline
    with_baseline = {f.rule_id for f in run_rules(drifted, previous=baseline)}
    assert "schema_drift" in with_baseline


# --- runner with an injected fake LLM ---

_FAKE_ANOMALY = {
    "negative_age": ("age", "logical"),
    "future_date": ("order_date", "logical"),
    "unit_mismatch": ("amount_usd", "logical"),
    "null_burst": ("email", "completeness"),
    "duplicate_key": ("order_id", "statistical"),
}


def _fake_llm_factory(model: str):
    """Perfect-on-profile fake: detects what a good analyst could see in the
    stats (5 classes), honestly misses schema_drift (needs the baseline the
    LLM never gets), and calls clean files clean."""

    def fake(profile: FileProfile, findings: list[RuleFinding]) -> LlmOutcome:
        label = Path(profile.source_uri).parent.name
        anomalies = []
        if label in _FAKE_ANOMALY:
            column, kind = _FAKE_ANOMALY[label]
            anomalies = [
                Anomaly(
                    column=column,
                    kind=kind,
                    severity="high",
                    explanation=f"synthetic {label}",
                    suspected_root_cause="synthetic",
                )
            ]
        report = AnomalyReport(
            data_quality_score=40 if anomalies else 95,
            anomalies=anomalies,
            summary="synthetic",
        )
        return LlmOutcome(
            status="ok",
            report=report,
            model=model,
            usage=LlmUsage(input_tokens=100, output_tokens=50),
            latency_ms=10.0,
        )

    return fake


@pytest.fixture(scope="module")
def results(corpus):
    manifest, out = corpus
    return run(manifest, out, list(ALL_ARMS), llm_factory=_fake_llm_factory, workers=2)


def _arm(results, name: str):
    return next(a for a in results.arms if a.arm == name)


def test_rules_only_arm_recall_split(results) -> None:
    arm = _arm(results, "rules_only")
    for cls in ("negative_age", "null_burst", "duplicate_key", "schema_drift"):
        assert arm.metrics[cls].recall == 1.0, cls
    for cls in ("future_date", "unit_mismatch"):
        assert arm.metrics[cls].recall == 0.0, cls
    assert arm.clean_fp_rate == 0.0
    assert arm.total_cost_usd == 0.0
    assert arm.model is None


def test_llm_only_arm_misses_drift(results) -> None:
    arm = _arm(results, "llm_only")
    assert arm.metrics["schema_drift"].recall == 0.0
    for cls in _FAKE_ANOMALY:
        assert arm.metrics[cls].recall == 1.0, cls
    assert arm.macro_recall == pytest.approx(5 / 6)


def test_hybrid_arm_is_the_union(results) -> None:
    arm = _arm(results, "hybrid")
    assert arm.macro_recall == 1.0
    assert arm.macro_precision == 1.0
    assert arm.macro_f1 == 1.0
    assert arm.clean_fp_rate == 0.0
    assert arm.llm_failures == 0
    assert arm.latency_p50_ms == pytest.approx(10.0)


def test_cost_uses_per_model_pricing(results) -> None:
    files = 4 + 2 * len(ANOMALY_CLASSES)  # every file gets one LLM call
    opus_per_call = (100 * 5.0 + 50 * 25.0) / 1e6
    sonnet_per_call = (100 * 3.0 + 50 * 15.0) / 1e6
    assert _arm(results, "hybrid").total_cost_usd == pytest.approx(
        files * opus_per_call, abs=1e-4
    )
    assert _arm(results, "hybrid_sonnet").total_cost_usd == pytest.approx(
        files * sonnet_per_call, abs=1e-4
    )
    assert _arm(results, "hybrid_sonnet").model == "claude-sonnet-5"


def test_render_results_markdown(results) -> None:
    text = render_results(results)
    assert "## hybrid" in text and "## rules_only" in text
    assert "| negative_age |" in text
    assert "**macro**" in text
    assert "Clean false-positive rate" in text


def test_llm_failure_counted_not_raised(corpus) -> None:
    manifest, out = corpus

    def broken_factory(model: str):
        def fake(profile, findings):
            return LlmOutcome(status="failed", failure_reason="api_key_missing", model=model)

        return fake

    res = run(manifest, out, ["llm_only"], llm_factory=broken_factory, workers=1)
    arm = res.arms[0]
    assert arm.llm_failures == len(manifest.files)
    assert arm.macro_recall == 0.0  # degraded, not crashed


# --- Batches API submission builder ---


def test_batch_requests_shape(corpus) -> None:
    manifest, out = corpus
    prepared = prepare_files(manifest, out)
    requests = build_batch_requests(prepared, model="claude-opus-4-8")

    assert len(requests) == len(manifest.files)
    ids = [r["custom_id"] for r in requests]
    assert len(set(ids)) == len(ids)

    params = requests[0]["params"]
    assert params["model"] == "claude-opus-4-8"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert '"profile"' in params["messages"][0]["content"]

    # The wire schema must not carry constraints the API rejects.
    schema_text = json.dumps(params["output_config"]["format"]["schema"])
    assert '"maximum"' not in schema_text and '"minimum"' not in schema_text
    assert '"additionalProperties": false' in schema_text
