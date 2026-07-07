"""Phase 2 live smoke test — one real Opus 4.8 call on the dirty fixture.

Costs a few cents; proves report quality before infrastructure gets built
around the client. Marked `live` (CI deselects with -m "not live") and skipped
automatically until ANTHROPIC_API_KEY is set.

Run explicitly with:
    pytest -m live -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_client import ClaudeAnalyzer
from profiler import profile_file
from rules_engine import run_rules

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — add the key to run the live smoke test",
    ),
]


def test_live_smoke_dirty_fixture_produces_sensible_report(fixtures_dir: Path) -> None:
    profile = profile_file(fixtures_dir / "dirty.csv")
    findings = run_rules(profile)
    outcome = ClaudeAnalyzer().analyze(profile, findings)

    assert outcome.status == "ok", f"live call failed: {outcome.failure_reason}"
    report = outcome.report
    assert report is not None

    # dirty.csv contains: duplicate ids, age -5 and 200, notes 75% null.
    # A sensible analysis flags at least one issue and scores well below clean.
    assert report.data_quality_score < 90
    assert len(report.anomalies) >= 1
    assert report.summary.strip()
    profile_columns = {c.name for c in profile.columns}
    for anomaly in report.anomalies:
        assert anomaly.column in profile_columns, f"hallucinated column: {anomaly.column!r}"

    # Human check: eyeball explanation quality with `pytest -m live -s`.
    print(f"\nscore={report.data_quality_score}  anomalies={len(report.anomalies)}")
    for a in report.anomalies:
        print(f"  [{a.severity}] {a.column} ({a.kind}): {a.explanation}")
        print(f"      root cause: {a.suspected_root_cause}")
    print(f"summary: {report.summary}")
    assert outcome.usage is not None
    cost = outcome.usage.input_tokens * 5 / 1e6 + outcome.usage.output_tokens * 25 / 1e6
    print(f"tokens: {outcome.usage.input_tokens} in / {outcome.usage.output_tokens} out")
    print(f"cost: ${cost:.4f}  latency: {outcome.latency_ms:.0f}ms")
