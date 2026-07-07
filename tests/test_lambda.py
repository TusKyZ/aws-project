"""End-to-end handler tests (Phase 4) — moto AWS, fake analyzer, zero spend.

The real DuckDB profiler can't reach moto's in-process S3, so `profile_file`
is redirected to the committed local fixtures (same code path from the profile
onward). The real S3 read is verified against the deployed stack in Phase 5.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import lambda_function
from lambda_function import dataset_for_key, lambda_handler, reset_caches
from models import LlmOutcome, LlmUsage
from profiler import profile_file as real_profile_file
from rules_engine import fallback_score
from tests_support import FakeContext

TABLE = "SentinelAuditLogs"
BUCKET = "data"


class FakeAnalyzer:
    def __init__(self, outcome_factory=None) -> None:
        self.calls: list[str] = []
        self._factory = outcome_factory or _ok_outcome

    def analyze(self, profile, findings):
        self.calls.append(profile.source_uri)
        return self._factory(profile, findings)


def _ok_outcome(profile, findings) -> LlmOutcome:
    from models import Anomaly, AnomalyReport

    return LlmOutcome(
        status="ok",
        report=AnomalyReport(
            data_quality_score=55,
            anomalies=[
                Anomaly(
                    column=profile.columns[0].name if profile.columns else "unknown",
                    kind="logical",
                    severity="high",
                    explanation="synthetic anomaly",
                    suspected_root_cause="synthetic",
                )
            ],
            summary="synthetic",
        ),
        model="claude-opus-4-8",
        usage=LlmUsage(input_tokens=100, output_tokens=50),
        latency_ms=12.0,
    )


def _failed_outcome(profile, findings) -> LlmOutcome:
    return LlmOutcome(status="failed", failure_reason="api_error: OverloadedError")


def _sqs_event(*bodies: dict) -> dict:
    return {
        "Records": [
            {"messageId": f"mid-{i}", "body": json.dumps(body)} for i, body in enumerate(bodies)
        ]
    }


def _s3_body(key: str, etag: str = "etag1") -> dict:
    return {
        "version": "0",
        "detail-type": "Object Created",
        "source": "aws.s3",
        "detail": {"bucket": {"name": BUCKET}, "object": {"key": key, "etag": etag}},
    }


@pytest.fixture()
def stack(aws_env, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path):
    """moto table/secret/topic + env config + fake profiler + fake analyzer."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("secretsmanager").create_secret(
            Name="anthropic_api_key", SecretString="sk-unused"
        )
        sns = boto3.client("sns")
        sqs = boto3.client("sqs")
        topic_arn = sns.create_topic(Name="alerts")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="alert-spy")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

        monkeypatch.setenv("TABLE_NAME", TABLE)
        monkeypatch.setenv("ANTHROPIC_SECRET_ID", "anthropic_api_key")
        monkeypatch.setenv("ALERT_TOPIC_ARN", topic_arn)
        monkeypatch.setenv("LLM_SKIP_ON_CLEAN", "true")
        reset_caches()

        # s3://data/<prefix>/<name> -> tests/fixtures/<name>
        def fake_profile(path, *, source_uri=None, file_format=None):
            name = str(path).rsplit("/", 1)[-1]
            return real_profile_file(
                fixtures_dir / name, source_uri=source_uri or str(path), file_format=file_format
            )

        monkeypatch.setattr(lambda_function, "profile_file", fake_profile)
        analyzer = FakeAnalyzer()
        monkeypatch.setattr(lambda_function, "get_analyzer", lambda: analyzer)

        yield {
            "table": dynamodb.Table(TABLE),
            "analyzer": analyzer,
            "sqs": sqs,
            "queue_url": queue_url,
            "monkeypatch": monkeypatch,
        }
        reset_caches()


def _alert_bodies(stack) -> list[str]:
    response = stack["sqs"].receive_message(
        QueueUrl=stack["queue_url"], MaxNumberOfMessages=10, WaitTimeSeconds=0
    )
    return [json.loads(m["Body"])["Message"] for m in response.get("Messages", [])]


def _item(stack, pk: str) -> dict | None:
    return stack["table"].get_item(Key={"pk": pk}).get("Item")


def test_dirty_file_end_to_end(stack) -> None:
    result = lambda_handler(_sqs_event(_s3_body("orders/dirty.csv")), FakeContext())

    assert result == {"batchItemFailures": []}
    item = _item(stack, f"s3://{BUCKET}/orders/dirty.csv#etag1")
    assert item is not None
    assert item["llm_status"] == "ok"
    assert int(item["data_quality_score"]) == 55
    assert "anomaly_report" in item
    assert stack["analyzer"].calls == [f"s3://{BUCKET}/orders/dirty.csv"]
    assert _item(stack, "LATEST#orders") is not None  # drift baseline written

    alerts = _alert_bodies(stack)
    assert len(alerts) == 1 and "orders/dirty.csv" in alerts[0]


def test_duplicate_delivery_runs_llm_once(stack) -> None:
    event = _sqs_event(_s3_body("orders/dirty.csv"))
    assert lambda_handler(event, FakeContext()) == {"batchItemFailures": []}
    assert lambda_handler(event, FakeContext()) == {"batchItemFailures": []}

    assert len(stack["analyzer"].calls) == 1  # no duplicate paid call
    scan = stack["table"].scan()
    pks = {i["pk"] for i in scan["Items"]}
    assert pks == {f"s3://{BUCKET}/orders/dirty.csv#etag1", "LATEST#orders"}


def test_unparseable_file_is_a_finding_not_a_failure(stack) -> None:
    result = lambda_handler(_sqs_event(_s3_body("raw/corrupt.parquet")), FakeContext())

    assert result == {"batchItemFailures": []}  # never DLQ for bad data
    item = _item(stack, f"s3://{BUCKET}/raw/corrupt.parquet#etag1")
    assert item["profile_status"] == "unparseable"
    assert int(item["data_quality_score"]) == 0
    assert item["llm_status"] == "skipped"
    assert stack["analyzer"].calls == []
    assert _item(stack, "LATEST#raw") is None  # no baseline from a broken file
    assert len(_alert_bodies(stack)) == 1  # unreadable file is alert-worthy


def test_skip_on_clean_saves_the_llm_call(stack) -> None:
    lambda_handler(_sqs_event(_s3_body("orders/clean.csv")), FakeContext())

    item = _item(stack, f"s3://{BUCKET}/orders/clean.csv#etag1")
    assert item["llm_status"] == "skipped"
    assert int(item["data_quality_score"]) == 100  # fallback score, no findings
    assert stack["analyzer"].calls == []
    assert _item(stack, "LATEST#orders") is not None


def test_llm_failure_degrades_with_fallback_score(stack) -> None:
    broken = FakeAnalyzer(_failed_outcome)
    stack["monkeypatch"].setattr(lambda_function, "get_analyzer", lambda: broken)

    result = lambda_handler(_sqs_event(_s3_body("orders/dirty.csv")), FakeContext())

    assert result == {"batchItemFailures": []}  # degradation, not DLQ
    item = _item(stack, f"s3://{BUCKET}/orders/dirty.csv#etag1")
    assert item["llm_status"] == "failed"
    assert item["failure_reason"] == "api_error: OverloadedError"
    # dirty.csv: duplicate_key + range_violation + null_threshold, all high.
    assert int(item["data_quality_score"]) == 25  # 100 - 3 * 25


def test_partial_batch_failure_isolated(stack) -> None:
    good = _s3_body("orders/clean.csv")
    malformed = {"detail": {"bucket": {"name": BUCKET}}}  # no object -> ValidationError

    result = lambda_handler(_sqs_event(good, malformed), FakeContext())

    assert result == {"batchItemFailures": [{"itemIdentifier": "mid-1"}]}
    assert _item(stack, f"s3://{BUCKET}/orders/clean.csv#etag1") is not None


def test_unsupported_suffix_skipped_quietly(stack) -> None:
    result = lambda_handler(_sqs_event(_s3_body("junk/readme.png")), FakeContext())

    assert result == {"batchItemFailures": []}
    assert stack["table"].scan()["Count"] == 0
    assert stack["analyzer"].calls == []


def test_schema_drift_detected_on_second_file(stack) -> None:
    lambda_handler(_sqs_event(_s3_body("orders/clean.csv", etag="e1")), FakeContext())
    # clean.json has different columns than clean.csv -> drift vs the baseline.
    lambda_handler(_sqs_event(_s3_body("orders/clean.json", etag="e2")), FakeContext())

    item = _item(stack, f"s3://{BUCKET}/orders/clean.json#e2")
    findings = json.loads(item["rule_findings"])
    assert "schema_drift" in {f["rule_id"] for f in findings}


def test_dataset_for_key() -> None:
    assert dataset_for_key("orders/2026/07/file.csv", 1) == "orders"
    assert dataset_for_key("orders/2026/07/file.csv", 2) == "orders/2026"
    assert dataset_for_key("file.csv", 1) == "_root"


def test_fallback_score_bands() -> None:
    from models import RuleFinding

    high = RuleFinding(rule_id="x", severity="high", message="m")
    medium = RuleFinding(rule_id="y", severity="medium", message="m")
    assert fallback_score([]) == 100
    assert fallback_score([high]) == 75
    assert fallback_score([high, medium]) == 65
    assert fallback_score([high] * 10) == 5  # floored above unparseable's 0
