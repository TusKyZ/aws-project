"""Lambda handler (Phase 4): SQS batch -> per-file DQ pipeline.

Flow per record: parse the EventBridge S3 event -> duplicate check (skip the
paid LLM call on redelivery) -> DuckDB profile via s3:// -> rules engine (with
the drift baseline from DynamoDB) -> Claude analysis (unless skip-on-clean or
degraded) -> conditional audit write -> baseline update -> high-severity alert.

Failure philosophy (matches implementation_plan.md):
- Unparseable file        -> audit record with score 0. Success, never DLQ.
- LLM layer down/failed   -> audit record with llm_status=failed + fallback
                             score from the rules. Success, never DLQ.
- Malformed event / AWS
  errors (Dynamo, etc.)   -> the record's messageId is reported via
                             ReportBatchItemFailures -> SQS retry -> DLQ after
                             max receives. That is what the DLQ is for.

Telemetry is AWS Lambda Powertools end to end: structured JSON logs keyed by
the SQS message id, custom metrics via EMF (no PutMetricData calls, no extra
IAM permission).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from functools import lru_cache
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import BaseModel, ConfigDict, Field

from alerting import AlertSender
from audit_writer import AuditWriter
from claude_client import DEFAULT_MODEL, ClaudeAnalyzer, estimate_cost_usd
from models import AuditRecord, FileProfile, LlmOutcome, ProfileStatus, RuleFinding
from profiler import profile_file
from rules_engine import fallback_score, run_rules
from secrets_provider import SecretsManagerProvider

logger = Logger(service="sentinel")
metrics = Metrics(namespace="Sentinel", service="sentinel")

_SUPPORTED_SUFFIXES = (".csv", ".parquet", ".json")


class HandlerConfig(BaseModel):
    """All runtime knobs, injected by Terraform as environment variables."""

    model_config = ConfigDict(extra="forbid")

    table_name: str
    anthropic_secret_id: str
    slack_secret_id: str | None = None
    alert_topic_arn: str | None = None
    audit_retention_days: int = Field(default=90, ge=1)
    llm_skip_on_clean: bool = True
    dataset_prefix_depth: int = Field(default=1, ge=1)
    llm_model: str = DEFAULT_MODEL


class S3Object(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    etag: str


class S3Bucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class S3EventDetail(BaseModel):
    """The `detail` of an EventBridge "Object Created" event."""

    model_config = ConfigDict(extra="ignore")

    bucket: S3Bucket
    object: S3Object


@lru_cache(maxsize=1)
def get_config() -> HandlerConfig:
    return HandlerConfig(
        table_name=os.environ["TABLE_NAME"],
        anthropic_secret_id=os.environ["ANTHROPIC_SECRET_ID"],
        slack_secret_id=os.environ.get("SLACK_SECRET_ID") or None,
        alert_topic_arn=os.environ.get("ALERT_TOPIC_ARN") or None,
        audit_retention_days=int(os.environ.get("AUDIT_RETENTION_DAYS", "90")),
        llm_skip_on_clean=os.environ.get("LLM_SKIP_ON_CLEAN", "true").lower() == "true",
        dataset_prefix_depth=int(os.environ.get("DATASET_PREFIX_DEPTH", "1")),
        llm_model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )


@lru_cache(maxsize=1)
def get_writer() -> AuditWriter:
    return AuditWriter(get_config().table_name)


@lru_cache(maxsize=1)
def get_analyzer() -> ClaudeAnalyzer:
    config = get_config()
    return ClaudeAnalyzer(
        SecretsManagerProvider(config.anthropic_secret_id), model=config.llm_model
    )


@lru_cache(maxsize=1)
def get_alerter() -> AlertSender:
    config = get_config()
    webhook = (
        SecretsManagerProvider(config.slack_secret_id) if config.slack_secret_id else None
    )
    return AlertSender(topic_arn=config.alert_topic_arn, webhook_provider=webhook)


def reset_caches() -> None:
    """Test seam: drop memoized config/clients between scenarios."""
    for cached in (get_config, get_writer, get_analyzer, get_alerter):
        clear = getattr(cached, "cache_clear", None)
        if clear is not None:  # a test may have monkeypatched the factory away
            clear()


def dataset_for_key(key: str, depth: int) -> str:
    """Dataset identity = leading key prefix (drives the drift baseline)."""
    parts = [p for p in key.split("/")[:-1] if p]
    return "/".join(parts[:depth]) if parts else "_root"


@logger.inject_lambda_context
@metrics.log_metrics(capture_cold_start_metric=True, raise_on_empty_metrics=False)
def lambda_handler(event: dict, context: Any) -> dict:
    """SQS batch entrypoint with partial-batch failure reporting."""
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        logger.append_keys(message_id=message_id)
        try:
            process_record(json.loads(record["body"]))
        except Exception:
            # Genuine processing failure: let SQS redeliver; DLQ after max
            # receives. Data-quality problems never take this path.
            logger.exception("record failed; reporting for retry")
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def process_record(body: dict) -> None:
    """One S3 object end to end. Raises only on infrastructure errors."""
    config = get_config()
    detail = S3EventDetail.model_validate(body["detail"])
    bucket, key, etag = detail.bucket.name, detail.object.key, detail.object.etag
    uri = f"s3://{bucket}/{key}"
    pk = f"{uri}#{etag}"
    logger.append_keys(source_uri=uri)

    if not key.lower().endswith(_SUPPORTED_SUFFIXES):
        # EventBridge already filters suffixes; this is defense in depth.
        logger.warning("unsupported suffix reached the handler; skipping")
        return

    writer = get_writer()
    if writer.exists(pk):
        logger.info("duplicate delivery; skipping (idempotent)")
        metrics.add_metric(name="DuplicateSkipped", unit=MetricUnit.Count, value=1)
        return

    started = dt.datetime.now(dt.UTC)
    profile = profile_file(uri, source_uri=uri)
    profile_ms = (dt.datetime.now(dt.UTC) - started).total_seconds() * 1000.0
    metrics.add_metric(name="ProfileDurationMs", unit=MetricUnit.Milliseconds, value=profile_ms)

    dataset = dataset_for_key(key, config.dataset_prefix_depth)
    previous = (
        writer.get_latest_profile(dataset)
        if profile.profile_status is ProfileStatus.OK
        else None
    )
    findings = run_rules(profile, previous=previous)

    outcome = _analyze(config, profile, findings)
    record = _build_record(config, pk, uri, etag, profile, findings, outcome, started)

    if not writer.write(record):
        logger.info("lost idempotency race; record already written")
        metrics.add_metric(name="DuplicateSkipped", unit=MetricUnit.Count, value=1)
        return

    if profile.profile_status is ProfileStatus.OK:
        writer.put_latest_profile(dataset, profile, record.created_at)

    _emit_metrics(record, outcome)

    if _needs_alert(record):
        result = get_alerter().send_high_severity(record)
        logger.info("alert dispatched", sns=result.sns_sent, slack=result.slack_sent)


def _analyze(
    config: HandlerConfig, profile: FileProfile, findings: list[RuleFinding]
) -> LlmOutcome | None:
    """None = deliberately skipped (unparseable file, or clean + skip flag)."""
    if profile.profile_status is not ProfileStatus.OK:
        return None
    if config.llm_skip_on_clean and not findings and not profile.partial_profile:
        return None
    return get_analyzer().analyze(profile, findings)


def _build_record(
    config: HandlerConfig,
    pk: str,
    uri: str,
    etag: str,
    profile: FileProfile,
    findings: list[RuleFinding],
    outcome: LlmOutcome | None,
    started: dt.datetime,
) -> AuditRecord:
    if profile.profile_status is not ProfileStatus.OK:
        score = 0  # unparseable is the worst possible data-quality result
    elif outcome is not None and outcome.status == "ok" and outcome.report is not None:
        score = outcome.report.data_quality_score
    else:
        score = fallback_score(findings)

    return AuditRecord(
        pk=pk,
        source_uri=uri,
        etag=etag,
        profile_status=profile.profile_status,
        partial_profile=profile.partial_profile,
        data_quality_score=score,
        rule_findings=findings,
        anomaly_report=(
            outcome.report if outcome is not None and outcome.status == "ok" else None
        ),
        llm_status="skipped" if outcome is None else outcome.status,
        failure_reason=(
            profile.error
            if profile.profile_status is not ProfileStatus.OK
            else (outcome.failure_reason if outcome is not None else None)
        ),
        created_at=started.isoformat(),
        expires_at=int(started.timestamp()) + config.audit_retention_days * 86_400,
    )


def _emit_metrics(record: AuditRecord, outcome: LlmOutcome | None) -> None:
    metrics.add_metric(
        name="DataQualityScore", unit=MetricUnit.Count, value=record.data_quality_score
    )
    metrics.add_metric(
        name="RuleFindingCount", unit=MetricUnit.Count, value=len(record.rule_findings)
    )
    if outcome is not None:
        if outcome.latency_ms is not None:
            metrics.add_metric(
                name="LlmLatencyMs", unit=MetricUnit.Milliseconds, value=outcome.latency_ms
            )
        metrics.add_metric(
            name="LlmCostUsd",
            unit=MetricUnit.Count,
            value=estimate_cost_usd(outcome.model, outcome.usage),
        )
        if outcome.status == "failed":
            metrics.add_metric(name="LlmFailureCount", unit=MetricUnit.Count, value=1)
    if record.anomaly_report is not None:
        metrics.add_metric(
            name="AnomalyCount",
            unit=MetricUnit.Count,
            value=len(record.anomaly_report.anomalies),
        )


def _needs_alert(record: AuditRecord) -> bool:
    if record.profile_status is not ProfileStatus.OK:
        return True  # a file we couldn't read at all is always alert-worthy
    if any(f.severity == "high" for f in record.rule_findings):
        return True
    return record.anomaly_report is not None and any(
        a.severity == "high" for a in record.anomaly_report.anomalies
    )
