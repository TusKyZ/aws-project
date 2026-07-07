"""High-severity alert publication (Phase 4/5): SNS topic + Slack webhook.

Both channels are optional and independent; alerting never raises — a broken
webhook must not fail the invocation (the audit record is already written).
The Slack webhook URL is a secret (same out-of-band treatment as the API key)
and arrives through a provider with the `get()` surface, so tests and the
env-var local path plug in identically.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

import boto3
from pydantic import BaseModel, ConfigDict

from models import AuditRecord


class ValueProvider(Protocol):
    def get(self) -> str | None: ...


class AlertResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sns_sent: bool = False
    slack_sent: bool = False
    errors: list[str] = []


def _default_post(url: str, body: bytes) -> int:
    request = urllib.request.Request(  # noqa: S310 - URL comes from Secrets Manager, not user input
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return int(response.status)


class AlertSender:
    def __init__(
        self,
        topic_arn: str | None = None,
        webhook_provider: ValueProvider | None = None,
        sns_client: Any | None = None,
        post: Callable[[str, bytes], int] = _default_post,
    ) -> None:
        self._topic_arn = topic_arn
        self._webhook_provider = webhook_provider
        self._sns = sns_client
        self._post = post

    def send_high_severity(self, record: AuditRecord) -> AlertResult:
        result = AlertResult()
        text = _format_alert(record)

        if self._topic_arn:
            try:
                sns = self._sns or boto3.client("sns")
                sns.publish(
                    TopicArn=self._topic_arn,
                    Subject=f"[Sentinel] DQ score {record.data_quality_score}"[:100],
                    Message=text,
                )
                result.sns_sent = True
            except Exception as exc:  # alerting is best-effort by contract
                result.errors.append(f"sns: {type(exc).__name__}")

        webhook = self._webhook_provider.get() if self._webhook_provider else None
        if webhook:
            try:
                status = self._post(webhook, json.dumps({"text": text}).encode("utf-8"))
                result.slack_sent = 200 <= status < 300
                if not result.slack_sent:
                    result.errors.append(f"slack: HTTP {status}")
            except Exception as exc:
                result.errors.append(f"slack: {type(exc).__name__}")
        return result


def _format_alert(record: AuditRecord) -> str:
    lines = [
        "Sentinel data-quality alert (high severity)",
        f"file: {record.source_uri}",
        f"score: {record.data_quality_score}/100  llm: {record.llm_status}",
    ]
    for finding in record.rule_findings:
        if finding.severity == "high":
            lines.append(f"- [rule] {finding.rule_id}: {finding.message}")
    if record.anomaly_report is not None:
        for anomaly in record.anomaly_report.anomalies:
            if anomaly.severity == "high":
                lines.append(f"- [llm] {anomaly.column} ({anomaly.kind}): {anomaly.explanation}")
        lines.append(f"summary: {record.anomaly_report.summary}")
    return "\n".join(lines)
