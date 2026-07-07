"""Claude Opus 4.8 analysis client (Phase 2).

Wraps `client.messages.parse()` with the `AnomalyReport` structured-output
contract. The one hard guarantee: `analyze()` never raises. Every failure mode
(missing key, 401, API error, refusal, schema-invalid response) degrades into
`LlmOutcome(status="failed", failure_reason=...)` so the pipeline writes an
audit record instead of retrying a poison message into the DLQ.

Key handling is a pluggable provider: locally `EnvKeyProvider` reads
ANTHROPIC_API_KEY; Phase 4 swaps in a Secrets Manager provider with the same
two-method surface (`get` / `invalidate`) to enable the 401 -> re-fetch ->
retry rotation path without touching this module.
"""

from __future__ import annotations

import json
import os
import time
from typing import Protocol

import anthropic
import httpx
import pydantic

from models import AnomalyReport, FileProfile, LlmOutcome, LlmUsage, RuleFinding
from prompts import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 16_000
# Keep the HTTP budget comfortably under the 300s Lambda timeout so a hung
# request degrades via LlmOutcome instead of killing the invocation.
DEFAULT_TIMEOUT_SECONDS = 120.0


class KeyProvider(Protocol):
    """Where API keys come from. Phase 4 adds a Secrets Manager implementation."""

    def get(self) -> str | None: ...

    def invalidate(self) -> None: ...


class EnvKeyProvider:
    """Local/dev provider: reads the key from an environment variable."""

    def __init__(self, var: str = "ANTHROPIC_API_KEY") -> None:
        self._var = var

    def get(self) -> str | None:
        value = os.environ.get(self._var, "").strip()
        return value or None

    def invalidate(self) -> None:
        # Nothing cached — the environment is re-read on every get().
        return None


class ClaudeAnalyzer:
    """Turns a FileProfile + rule findings into a validated AnomalyReport."""

    def __init__(
        self,
        key_provider: KeyProvider | None = None,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._key_provider = key_provider or EnvKeyProvider()
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client
        self._client: anthropic.Anthropic | None = None
        self._client_key: str | None = None

    def analyze(
        self, profile: FileProfile, findings: list[RuleFinding] | None = None
    ) -> LlmOutcome:
        """Run one analysis. Never raises — failures become LlmOutcome(failed)."""
        start = time.perf_counter()

        def failed(reason: str) -> LlmOutcome:
            return LlmOutcome(
                status="failed",
                failure_reason=reason,
                model=self._model,
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )

        user_payload = _build_payload(profile, findings or [])

        # Attempt 1 with the current key; on a 401, invalidate the cached key
        # (a rotated secret heals warm containers) and retry exactly once.
        response = None
        for _attempt in range(2):
            client = self._get_client()
            if client is None:
                return failed("api_key_missing")
            try:
                response = client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    thinking={"type": "adaptive"},
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_payload}],
                    output_format=AnomalyReport,
                )
                break
            except anthropic.AuthenticationError:
                self._key_provider.invalidate()
                self._client = None
                continue
            except pydantic.ValidationError as exc:
                # The API answered but the body doesn't satisfy AnomalyReport.
                first = exc.errors()[0]
                return failed(f"invalid_response: {first['type']} at {list(first['loc'])}")
            except anthropic.APIError as exc:
                return failed(f"api_error: {type(exc).__name__}")
            except Exception as exc:  # never let the LLM layer take down the pipeline
                return failed(f"unexpected: {type(exc).__name__}")
        if response is None:
            return failed("authentication_error")

        if response.stop_reason == "refusal" or response.parsed_output is None:
            return failed("refusal")

        return LlmOutcome(
            status="ok",
            report=response.parsed_output,
            model=response.model,
            usage=LlmUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            latency_ms=(time.perf_counter() - start) * 1000.0,
        )

    def _get_client(self) -> anthropic.Anthropic | None:
        """Build (and cache per key) the SDK client; None when no key exists."""
        key = self._key_provider.get()
        if key is None:
            return None
        if self._client is None or key != self._client_key:
            self._client = anthropic.Anthropic(
                api_key=key,
                timeout=self._timeout,
                max_retries=self._max_retries,
                http_client=self._http_client,
            )
            self._client_key = key
        return self._client


def _build_payload(profile: FileProfile, findings: list[RuleFinding]) -> str:
    """Serialize the analysis input deterministically (stable keys, no NaN)."""
    return json.dumps(
        {
            "profile": profile.model_dump(mode="json"),
            "rule_findings": [f.model_dump(mode="json") for f in findings],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
