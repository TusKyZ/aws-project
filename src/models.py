"""Pydantic contracts — the spine every later phase builds on.

Profiling output (`FileProfile`/`ColumnProfile`), deterministic findings
(`RuleFinding`), the LLM output contract (`AnomalyReport`/`Anomaly`, used in
Phase 2), and the DynamoDB audit-record shape (`AuditRecord`, written in
Phase 4/5). Defining all of them now keeps the data flow typed end to end.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["low", "medium", "high"]


class ProfileStatus(StrEnum):
    """Whether DuckDB could read the file at all."""

    OK = "ok"
    UNPARSEABLE = "unparseable"


class SemanticType(StrEnum):
    """DuckDB types collapsed into the categories the rules engine reasons about."""

    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    BOOLEAN = "boolean"
    STRING = "string"
    OTHER = "other"  # complex types: struct / list / map / ...


class ColumnProfile(BaseModel):
    """Per-column statistics from a single DuckDB pass."""

    model_config = ConfigDict(extra="forbid")

    name: str
    duckdb_type: str
    semantic_type: SemanticType
    null_count: int = Field(ge=0)
    null_pct: float = Field(ge=0.0, le=1.0)
    distinct_count: int = Field(ge=0, description="Exact non-null distinct count.")
    # String repr for display / LLM context (works for any type).
    min_value: str | None = None
    max_value: str | None = None
    # Populated only for numeric columns, so the rules engine can do range math.
    numeric_min: float | None = None
    numeric_max: float | None = None


class FileProfile(BaseModel):
    """File-level profile: the input to both the rules engine and the LLM."""

    model_config = ConfigDict(extra="forbid")

    source_uri: str
    file_format: Literal["csv", "parquet", "json"]
    profile_status: ProfileStatus = ProfileStatus.OK
    partial_profile: bool = False
    row_count: int = Field(default=0, ge=0)
    column_count: int = Field(default=0, ge=0)
    columns: list[ColumnProfile] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Set when profile_status is unparseable.")

    def column(self, name: str) -> ColumnProfile | None:
        """Case-insensitive column lookup."""
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


class RuleFinding(BaseModel):
    """One deterministic data-quality finding. `column` is None for file-level findings."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    severity: Severity
    message: str
    column: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Anomaly(BaseModel):
    """One LLM-surfaced anomaly (Phase 2)."""

    model_config = ConfigDict(extra="forbid")

    column: str
    kind: Literal["logical", "statistical", "schema", "completeness"]
    severity: Severity
    explanation: str
    suspected_root_cause: str


class AnomalyReport(BaseModel):
    """Structured LLM output contract enforced via `messages.parse()` (Phase 2)."""

    model_config = ConfigDict(extra="forbid")

    data_quality_score: int = Field(ge=0, le=100)
    anomalies: list[Anomaly] = Field(default_factory=list)
    summary: str


class LlmUsage(BaseModel):
    """Token usage from a single API call — the raw material for LlmCostUsd."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class LlmOutcome(BaseModel):
    """Result of one LLM analysis attempt (Phase 2).

    The client never raises: any failure (missing key, auth, API error,
    schema-invalid response, refusal) becomes `status="failed"` with a
    `failure_reason`, so the pipeline degrades instead of retrying into
    the DLQ. Feeds `AuditRecord.llm_status` / `failure_reason`.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    report: AnomalyReport | None = None
    failure_reason: str | None = None
    model: str | None = None
    usage: LlmUsage | None = None
    latency_ms: float | None = Field(default=None, ge=0.0)


class AuditRecord(BaseModel):
    """DynamoDB item shape for the audit log (persisted in Phase 4/5).

    `pk` is `s3://bucket/key#etag`; the conditional write on `pk` is what makes
    redelivered events idempotent. `expires_at` is epoch seconds for TTL, and is
    None for the TTL-exempt `LATEST#<dataset>` drift-baseline items.
    """

    model_config = ConfigDict(extra="forbid")

    pk: str
    source_uri: str
    etag: str
    profile_status: ProfileStatus
    partial_profile: bool = False
    data_quality_score: int = Field(ge=0, le=100)
    rule_findings: list[RuleFinding] = Field(default_factory=list)
    anomaly_report: AnomalyReport | None = None
    llm_status: Literal["ok", "failed", "skipped"] = "skipped"
    failure_reason: str | None = None
    created_at: str  # ISO 8601 UTC
    expires_at: int | None = None
