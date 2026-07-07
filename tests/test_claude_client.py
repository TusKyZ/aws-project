"""Contract tests for the Claude client (Phase 2).

All tests run against recorded API response shapes served through an
httpx.MockTransport — the real SDK request/parse path is exercised with zero
network and zero spend. Response shapes were captured from a probe run against
anthropic 0.116.0 (see git history of Phase 2).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from claude_client import DEFAULT_MODEL, ClaudeAnalyzer, EnvKeyProvider
from models import FileProfile, RuleFinding
from profiler import profile_file
from prompts import SYSTEM_PROMPT
from rules_engine import run_rules

GOOD_REPORT = {
    "data_quality_score": 42,
    "anomalies": [
        {
            "column": "age",
            "kind": "logical",
            "severity": "high",
            "explanation": "Age of -5 is impossible for a person.",
            "suspected_root_cause": "Sign flipped during ingestion.",
        }
    ],
    "summary": "One impossible age value detected.",
}


def _response_body(
    text: str = "",
    stop_reason: str = "end_turn",
    content: list | None = None,
) -> dict:
    """Recorded /v1/messages response shape (anthropic 0.116.0)."""
    if content is None:
        content = [{"type": "text", "text": text}]
    return {
        "id": "msg_test123",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 111, "output_tokens": 22},
    }


AUTH_ERROR_BODY = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "invalid x-api-key"},
}


class RecordingProvider:
    """Key provider that serves keys in order and records invalidations."""

    def __init__(self, *keys: str | None) -> None:
        self._keys = list(keys)
        self._index = 0
        self.invalidations = 0

    def get(self) -> str | None:
        return self._keys[min(self._index, len(self._keys) - 1)]

    def invalidate(self) -> None:
        self.invalidations += 1
        self._index += 1


def _analyzer(handler, provider=None, **kwargs) -> tuple[ClaudeAnalyzer, list[httpx.Request]]:
    """Build an analyzer whose HTTP layer is the given handler; capture requests."""
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    analyzer = ClaudeAnalyzer(
        provider or RecordingProvider("sk-test-key"),
        http_client=httpx.Client(transport=httpx.MockTransport(recording_handler)),
        max_retries=0,
        **kwargs,
    )
    return analyzer, seen


@pytest.fixture(scope="module")
def dirty_profile(fixtures_dir: Path) -> FileProfile:
    return profile_file(fixtures_dir / "dirty.csv")


@pytest.fixture(scope="module")
def dirty_findings(dirty_profile: FileProfile) -> list[RuleFinding]:
    return run_rules(dirty_profile)


def test_happy_path_returns_ok_outcome(dirty_profile, dirty_findings) -> None:
    analyzer, seen = _analyzer(
        lambda r: httpx.Response(200, json=_response_body(json.dumps(GOOD_REPORT)))
    )
    outcome = analyzer.analyze(dirty_profile, dirty_findings)

    assert outcome.status == "ok"
    assert outcome.failure_reason is None
    assert outcome.report is not None
    assert outcome.report.data_quality_score == 42
    assert outcome.report.anomalies[0].column == "age"
    assert outcome.usage is not None
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (111, 22)
    assert outcome.model == DEFAULT_MODEL
    assert outcome.latency_ms is not None and outcome.latency_ms >= 0.0

    # Request contract: model, adaptive thinking, schema-enforced output.
    body = json.loads(seen[0].content)
    assert body["model"] == DEFAULT_MODEL
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    # Untrusted file content rides in the user turn, never the system prompt.
    assert body["system"] == SYSTEM_PROMPT
    user_text = body["messages"][0]["content"]
    assert "dirty.csv" in user_text
    assert "rule_findings" in user_text


def test_missing_key_degrades_without_any_request(dirty_profile) -> None:
    analyzer, seen = _analyzer(
        lambda r: httpx.Response(200, json=_response_body(json.dumps(GOOD_REPORT))),
        provider=RecordingProvider(None),
    )
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "failed"
    assert outcome.failure_reason == "api_key_missing"
    assert outcome.report is None
    assert seen == []


def test_malformed_response_fails_closed(dirty_profile) -> None:
    analyzer, _ = _analyzer(lambda r: httpx.Response(200, json=_response_body("not { valid json")))
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "failed"
    assert outcome.report is None
    assert outcome.failure_reason is not None
    assert outcome.failure_reason.startswith("invalid_response")


def test_schema_violation_fails_closed(dirty_profile) -> None:
    bad = dict(GOOD_REPORT, data_quality_score=999)  # violates le=100
    analyzer, _ = _analyzer(lambda r: httpx.Response(200, json=_response_body(json.dumps(bad))))
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "failed"
    assert outcome.failure_reason is not None
    assert outcome.failure_reason.startswith("invalid_response")


def test_refusal_fails_closed(dirty_profile) -> None:
    analyzer, _ = _analyzer(
        lambda r: httpx.Response(200, json=_response_body(stop_reason="refusal", content=[]))
    )
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "failed"
    assert outcome.failure_reason == "refusal"
    assert outcome.report is None


def test_401_rotation_recovers_with_fresh_key(dirty_profile) -> None:
    provider = RecordingProvider("sk-stale", "sk-fresh")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-api-key"] == "sk-stale":
            return httpx.Response(401, json=AUTH_ERROR_BODY)
        return httpx.Response(200, json=_response_body(json.dumps(GOOD_REPORT)))

    analyzer, seen = _analyzer(handler, provider=provider)
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "ok"
    assert provider.invalidations == 1
    assert len(seen) == 2
    assert seen[0].headers["x-api-key"] == "sk-stale"
    assert seen[1].headers["x-api-key"] == "sk-fresh"


def test_401_twice_gives_up_after_one_retry(dirty_profile) -> None:
    provider = RecordingProvider("sk-bad-1", "sk-bad-2", "sk-bad-3")
    analyzer, seen = _analyzer(
        lambda r: httpx.Response(401, json=AUTH_ERROR_BODY), provider=provider
    )
    outcome = analyzer.analyze(dirty_profile)

    assert outcome.status == "failed"
    assert outcome.failure_reason == "authentication_error"
    assert len(seen) == 2  # exactly one retry, no loop
    assert provider.invalidations == 2


def test_api_error_degrades_not_raises(dirty_profile) -> None:
    analyzer, _ = _analyzer(
        lambda r: httpx.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
        )
    )
    outcome = analyzer.analyze(dirty_profile)  # must not raise

    assert outcome.status == "failed"
    assert outcome.failure_reason is not None
    assert "OverloadedError" in outcome.failure_reason


def test_system_prompt_scopes_file_content_as_untrusted() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "untrusted" in lowered
    assert "never" in lowered and "instructions" in lowered


def test_env_key_provider_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert EnvKeyProvider().get() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-from-env  ")
    assert EnvKeyProvider().get() == "sk-from-env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert EnvKeyProvider().get() is None
