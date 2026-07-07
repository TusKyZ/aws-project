"""Idempotent DynamoDB audit log (Phase 4).

Item layout (one table):

  pk = "s3://bucket/key#etag"   -> one audit record per unique object version.
       Scalars are stored as native attributes (queryable in the console);
       `rule_findings` and `anomaly_report` are stored as JSON strings —
       they're read back whole, and strings dodge DynamoDB's float/Decimal
       marshalling entirely.
  pk = "LATEST#<dataset>"       -> drift baseline: the last-seen FileProfile
       for a dataset, overwritten on every successful profile. Deliberately
       carries NO expires_at attribute (TTL-exempt), so drift detection
       survives retention expiry for datasets that go quiet.

Idempotency: `write()` uses ConditionExpression attribute_not_exists(pk) —
a redelivered SQS message becomes a no-op instead of a duplicate row. The
handler also calls `exists()` before the (paid) LLM call; the conditional
write remains the authoritative guard for the race window in between.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import TypeAdapter

from models import AnomalyReport, AuditRecord, FileProfile, RuleFinding

_FINDINGS_ADAPTER = TypeAdapter(list[RuleFinding])


class AuditWriter:
    def __init__(self, table_name: str, dynamodb: Any | None = None) -> None:
        self._table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)

    def exists(self, pk: str) -> bool:
        """Consistent point-read so a fast redelivery can't double-spend the LLM."""
        response = self._table.get_item(
            Key={"pk": pk}, ProjectionExpression="pk", ConsistentRead=True
        )
        return "Item" in response

    def write(self, record: AuditRecord) -> bool:
        """Conditionally persist the record. True = written, False = duplicate."""
        item: dict[str, Any] = {
            "pk": record.pk,
            "source_uri": record.source_uri,
            "etag": record.etag,
            "profile_status": record.profile_status.value,
            "partial_profile": record.partial_profile,
            "data_quality_score": record.data_quality_score,
            "llm_status": record.llm_status,
            "created_at": record.created_at,
            "rule_findings": _dump_list(record.rule_findings),
        }
        if record.failure_reason is not None:
            item["failure_reason"] = record.failure_reason
        if record.anomaly_report is not None:
            item["anomaly_report"] = record.anomaly_report.model_dump_json()
        # TTL attribute must be absent (not null) on records that never expire.
        if record.expires_at is not None:
            item["expires_at"] = record.expires_at
        try:
            self._table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(pk)"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get(self, pk: str) -> AuditRecord | None:
        """Read one audit record back into its typed form."""
        response = self._table.get_item(Key={"pk": pk})
        item = response.get("Item")
        if item is None:
            return None
        return AuditRecord(
            pk=item["pk"],
            source_uri=item["source_uri"],
            etag=item["etag"],
            profile_status=item["profile_status"],
            partial_profile=bool(item.get("partial_profile", False)),
            data_quality_score=int(item["data_quality_score"]),
            rule_findings=_load_findings(item.get("rule_findings", "[]")),
            anomaly_report=_load_report(item.get("anomaly_report")),
            llm_status=item.get("llm_status", "skipped"),
            failure_reason=item.get("failure_reason"),
            created_at=item["created_at"],
            expires_at=int(item["expires_at"]) if "expires_at" in item else None,
        )

    def get_latest_profile(self, dataset: str) -> FileProfile | None:
        """Drift baseline for a dataset, or None the first time it's seen."""
        response = self._table.get_item(Key={"pk": f"LATEST#{dataset}"})
        item = response.get("Item")
        if item is None:
            return None
        return FileProfile.model_validate_json(item["profile"])

    def put_latest_profile(self, dataset: str, profile: FileProfile, updated_at: str) -> None:
        """Overwrite the baseline (unconditional, TTL-exempt)."""
        self._table.put_item(
            Item={
                "pk": f"LATEST#{dataset}",
                "profile": profile.model_dump_json(),
                "updated_at": updated_at,
            }
        )


def _dump_list(findings: list[RuleFinding]) -> str:
    return _FINDINGS_ADAPTER.dump_json(findings).decode("utf-8")


def _load_findings(raw: str) -> list[RuleFinding]:
    return _FINDINGS_ADAPTER.validate_json(raw)


def _load_report(raw: str | None) -> AnomalyReport | None:
    return None if raw is None else AnomalyReport.model_validate_json(raw)
