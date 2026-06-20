"""Tests for the deterministic rules engine (Phase 1)."""

from __future__ import annotations

from pathlib import Path

from models import ColumnProfile, FileProfile, ProfileStatus, SemanticType
from profiler import profile_file
from rules_engine import (
    RangeRule,
    RulesConfig,
    check_schema_drift,
    run_rules,
)


def _finding_ids(findings) -> set[str]:
    return {f.rule_id for f in findings}


def test_clean_file_has_no_findings(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "clean.csv")
    assert run_rules(p) == []


def test_dirty_file_findings(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "dirty.csv")
    findings = run_rules(p)
    by_id = {f.rule_id: f for f in findings}

    assert _finding_ids(findings) == {"duplicate_key", "range_violation", "null_threshold"}
    assert by_id["duplicate_key"].column == "id"
    assert by_id["range_violation"].column == "age"
    assert by_id["null_threshold"].column == "notes"


def test_empty_file_flagged(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "empty.csv")
    findings = run_rules(p)
    assert "empty_file" in _finding_ids(findings)


def test_null_threshold_is_configurable(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "dirty.csv")
    # Lower threshold to 0.2 -> email (0.25) and age (0.25) now also flagged.
    findings = run_rules(p, RulesConfig(null_pct_threshold=0.2))
    null_cols = {f.column for f in findings if f.rule_id == "null_threshold"}
    assert null_cols == {"email", "age", "notes"}


def test_duplicate_key_not_flagged_when_unique(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "clean.csv")  # id is unique
    findings = run_rules(p)
    assert "duplicate_key" not in _finding_ids(findings)


def test_range_rule_configurable(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "clean.csv")  # ages 23..55
    # Impose a strict ceiling so a clean column now violates it.
    findings = run_rules(p, RulesConfig(range_rules=[RangeRule(column="age", min=0, max=40)]))
    rv = [f for f in findings if f.rule_id == "range_violation"]
    assert len(rv) == 1
    assert rv[0].column == "age"
    assert rv[0].details["observed_max"] == 55.0


# --- schema drift (pure function; DynamoDB fetch wired in Phase 5) ---


def _profile(columns: list[ColumnProfile]) -> FileProfile:
    return FileProfile(
        source_uri="s3://b/orders/f.csv",
        file_format="csv",
        profile_status=ProfileStatus.OK,
        row_count=10,
        column_count=len(columns),
        columns=columns,
    )


def _col(
    name: str, dtype: str = "BIGINT", sem: SemanticType = SemanticType.NUMERIC
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        duckdb_type=dtype,
        semantic_type=sem,
        null_count=0,
        null_pct=0.0,
        distinct_count=10,
    )


def test_drift_none_previous_is_empty() -> None:
    current = _profile([_col("id"), _col("age")])
    assert check_schema_drift(current, None) == []


def test_drift_identical_is_empty() -> None:
    cols = [_col("id"), _col("age")]
    assert check_schema_drift(_profile(cols), _profile(cols)) == []


def test_drift_added_and_removed_columns() -> None:
    prev = _profile([_col("id"), _col("age")])
    curr = _profile([_col("id"), _col("email", "VARCHAR", SemanticType.STRING)])
    findings = check_schema_drift(curr, prev)
    assert all(f.rule_id == "schema_drift" for f in findings)
    detail = findings[0].details
    assert "email" in detail["added"]
    assert "age" in detail["removed"]


def test_drift_type_change() -> None:
    prev = _profile([_col("id", "BIGINT", SemanticType.NUMERIC)])
    curr = _profile([_col("id", "VARCHAR", SemanticType.STRING)])
    findings = check_schema_drift(curr, prev)
    assert any(f.rule_id == "schema_drift" for f in findings)
    assert findings[0].details["retyped"]["id"] == {"from": "BIGINT", "to": "VARCHAR"}
