"""Secrets Manager value provider (Phase 4).

Implements the same two-method surface as `claude_client.EnvKeyProvider`
(`get` / `invalidate`), so the analyzer's 401 -> invalidate -> re-fetch ->
retry rotation path works unchanged in Lambda. Also used for the Slack
webhook URL.

The value is cached for the container lifetime (one GetSecretValue per cold
start, ~50ms + API cost saved on warm invocations). `invalidate()` drops the
cache so the next `get()` re-fetches — that is what lets a rotated key heal
warm containers without a redeploy.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError


class SecretsManagerProvider:
    """Cached SecretString lookup with explicit invalidation."""

    def __init__(self, secret_id: str, client: Any | None = None) -> None:
        self._secret_id = secret_id
        self._client = client or boto3.client("secretsmanager")
        self._cached: str | None = None
        self._fetched = False

    def get(self) -> str | None:
        """Return the secret value, or None when it can't be fetched.

        Returning None (instead of raising) is deliberate: a Secrets Manager
        outage or missing secret must degrade like any other LLM-layer failure
        (llm_status=failed), never crash the invocation into the DLQ.
        """
        if self._fetched:
            return self._cached
        try:
            response = self._client.get_secret_value(SecretId=self._secret_id)
            value = (response.get("SecretString") or "").strip()
            self._cached = value or None
        except ClientError:
            self._cached = None
        self._fetched = True
        return self._cached

    def invalidate(self) -> None:
        self._cached = None
        self._fetched = False
