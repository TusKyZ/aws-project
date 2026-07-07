"""AuditWriter tests (Phase 4) — moto DynamoDB, zero AWS spend."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from audit_writer import AuditWriter
from models import Anomaly, AnomalyReport, AuditRecord, FileProfile, ProfileStatus, RuleFinding

TABLE = "SentinelAuditLogs"


@pytest.fixture()
def writer(aws_env):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield AuditWriter(TABLE, dynamodb)


def _record(pk: str = "s3://data/orders/f.csv#etag1", expires_at: int | None = 1_800_000_000):
    return AuditRecord(
        pk=pk,
        source_uri=pk.rsplit("#", 1)[0],
        etag=pk.rsplit("#", 1)[1],
        profile_status=ProfileStatus.OK,
        data_quality_score=42,
        rule_findings=[
            RuleFinding(
                rule_id="range_violation",
                severity="high",
                message="Column 'age' out of range.",
                column="age",
                # Float in details on purpose: JSON-string storage must dodge
                # DynamoDB's float->Decimal marshalling.
                details={"observed_max": 200.5, "allowed_max": 150.0},
            )
        ],
        anomaly_report=AnomalyReport(
            data_quality_score=42,
            anomalies=[
                Anomaly(
                    column="age",
                    kind="logical",
                    severity="high",
                    explanation="Impossible age.",
                    suspected_root_cause="Sign flip.",
                )
            ],
            summary="One impossible age.",
        ),
        llm_status="ok",
        created_at="2026-07-08T00:00:00+00:00",
        expires_at=expires_at,
    )


def test_write_then_duplicate_is_noop(writer: AuditWriter) -> None:
    record = _record()
    assert writer.exists(record.pk) is False
    assert writer.write(record) is True
    assert writer.exists(record.pk) is True
    assert writer.write(record) is False  # idempotent: second delivery no-ops
    assert writer._table.scan()["Count"] == 1


def test_roundtrip_preserves_typed_record(writer: AuditWriter) -> None:
    record = _record()
    writer.write(record)
    loaded = writer.get(record.pk)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()
    assert loaded.rule_findings[0].details["observed_max"] == 200.5


def test_expires_at_none_means_attribute_absent(writer: AuditWriter) -> None:
    record = _record(pk="s3://data/orders/keep.csv#e", expires_at=None)
    writer.write(record)
    raw = writer._table.get_item(Key={"pk": record.pk})["Item"]
    assert "expires_at" not in raw  # TTL attr must be absent, not null


def test_latest_profile_roundtrip_and_overwrite(writer: AuditWriter) -> None:
    assert writer.get_latest_profile("orders") is None

    profile_v1 = FileProfile(source_uri="s3://data/orders/a.csv", file_format="csv", row_count=5)
    writer.put_latest_profile("orders", profile_v1, "2026-07-08T00:00:00+00:00")
    assert writer.get_latest_profile("orders") == profile_v1

    profile_v2 = FileProfile(source_uri="s3://data/orders/b.csv", file_format="csv", row_count=9)
    writer.put_latest_profile("orders", profile_v2, "2026-07-08T01:00:00+00:00")
    assert writer.get_latest_profile("orders") == profile_v2  # unconditional overwrite

    raw = writer._table.get_item(Key={"pk": "LATEST#orders"})["Item"]
    assert "expires_at" not in raw  # baseline is TTL-exempt by design
