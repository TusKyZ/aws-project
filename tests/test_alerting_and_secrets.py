"""SecretsManagerProvider + AlertSender tests (Phase 4) — moto, zero spend."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from alerting import AlertSender
from models import Anomaly, AnomalyReport, AuditRecord, ProfileStatus, RuleFinding
from secrets_provider import SecretsManagerProvider

# --- SecretsManagerProvider ---


@pytest.fixture()
def secrets(aws_env):
    with mock_aws():
        yield boto3.client("secretsmanager")


def test_secret_fetch_and_container_cache(secrets) -> None:
    secrets.create_secret(Name="anthropic_api_key", SecretString="sk-one")
    provider = SecretsManagerProvider("anthropic_api_key", secrets)

    assert provider.get() == "sk-one"
    secrets.put_secret_value(SecretId="anthropic_api_key", SecretString="sk-two")
    assert provider.get() == "sk-one"  # cached for the container lifetime


def test_invalidate_refetches_rotated_value(secrets) -> None:
    secrets.create_secret(Name="anthropic_api_key", SecretString="sk-one")
    provider = SecretsManagerProvider("anthropic_api_key", secrets)
    assert provider.get() == "sk-one"

    secrets.put_secret_value(SecretId="anthropic_api_key", SecretString="sk-two")
    provider.invalidate()
    assert provider.get() == "sk-two"  # the 401 rotation path in one line


def test_missing_secret_returns_none_never_raises(secrets) -> None:
    provider = SecretsManagerProvider("does-not-exist", secrets)
    assert provider.get() is None  # degrades like an LLM failure, no DLQ


def test_empty_secret_value_is_none(secrets) -> None:
    secrets.create_secret(Name="blank", SecretString="   ")
    assert SecretsManagerProvider("blank", secrets).get() is None


# --- AlertSender ---


def _record() -> AuditRecord:
    return AuditRecord(
        pk="s3://data/orders/f.csv#e1",
        source_uri="s3://data/orders/f.csv",
        etag="e1",
        profile_status=ProfileStatus.OK,
        data_quality_score=21,
        rule_findings=[
            RuleFinding(
                rule_id="duplicate_key",
                severity="high",
                message="Key-like column 'order_id' has duplicates.",
                column="order_id",
            )
        ],
        anomaly_report=AnomalyReport(
            data_quality_score=21,
            anomalies=[
                Anomaly(
                    column="age",
                    kind="logical",
                    severity="high",
                    explanation="Negative ages present.",
                    suspected_root_cause="Sign flip.",
                )
            ],
            summary="Multiple severe issues.",
        ),
        llm_status="ok",
        created_at="2026-07-08T00:00:00+00:00",
        expires_at=None,
    )


class _StaticProvider:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self) -> str | None:
        return self._value


def test_sns_alert_delivered(aws_env) -> None:
    with mock_aws():
        sns = boto3.client("sns")
        sqs = boto3.client("sqs")
        topic_arn = sns.create_topic(Name="sentinel-alerts")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="spy")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

        result = AlertSender(topic_arn=topic_arn, sns_client=sns).send_high_severity(_record())

        assert result.sns_sent is True and result.errors == []
        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)["Messages"]
        body = json.loads(messages[0]["Body"])["Message"]
        assert "s3://data/orders/f.csv" in body
        assert "[rule] duplicate_key" in body
        assert "[llm] age" in body


def test_slack_alert_posts_json(aws_env) -> None:
    posts: list[tuple[str, bytes]] = []

    def fake_post(url: str, body: bytes) -> int:
        posts.append((url, body))
        return 200

    sender = AlertSender(
        webhook_provider=_StaticProvider("https://hooks.slack.example/T000/B000/x"),
        post=fake_post,
    )
    result = sender.send_high_severity(_record())

    assert result.slack_sent is True
    url, body = posts[0]
    assert url.startswith("https://hooks.slack.example/")
    assert "score: 21/100" in json.loads(body)["text"]


def test_alerting_never_raises(aws_env) -> None:
    def exploding_post(url: str, body: bytes) -> int:
        raise OSError("connection refused")

    with mock_aws():
        sender = AlertSender(
            topic_arn="arn:aws:sns:us-east-1:123456789012:does-not-exist",
            webhook_provider=_StaticProvider("https://hooks.slack.example/x"),
            post=exploding_post,
        )
        result = sender.send_high_severity(_record())  # must not raise

    assert result.sns_sent is False and result.slack_sent is False
    assert len(result.errors) == 2


def test_no_channels_configured_is_a_quiet_noop(aws_env) -> None:
    result = AlertSender().send_high_severity(_record())
    assert result == type(result)()  # all defaults: nothing sent, no errors
