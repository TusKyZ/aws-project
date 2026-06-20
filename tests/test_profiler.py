"""Tests for the DuckDB profiler against committed fixtures (Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

from models import ProfileStatus, SemanticType
from profiler import profile_file


def test_clean_csv_profile(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "clean.csv")

    assert p.profile_status is ProfileStatus.OK
    assert p.file_format == "csv"
    assert p.row_count == 5
    assert p.column_count == 5
    assert {c.name for c in p.columns} == {"id", "age", "email", "signup_date", "balance"}
    # Clean file: zero nulls everywhere.
    assert all(c.null_count == 0 and c.null_pct == 0.0 for c in p.columns)

    idc = p.column("id")
    assert idc is not None
    assert idc.semantic_type is SemanticType.NUMERIC
    assert idc.distinct_count == 5  # all unique

    age = p.column("age")
    assert age is not None
    assert age.numeric_min == 23.0
    assert age.numeric_max == 55.0

    email = p.column("email")
    assert email is not None
    assert email.semantic_type is SemanticType.STRING


def test_dirty_csv_profile(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "dirty.csv")

    assert p.profile_status is ProfileStatus.OK
    assert p.row_count == 4

    idc = p.column("id")
    assert idc is not None
    assert idc.null_count == 0
    assert idc.distinct_count == 3  # values 1,1,2,3 -> duplicate present

    age = p.column("age")
    assert age is not None
    assert age.null_count == 1
    assert age.null_pct == 0.25
    assert age.numeric_min == -5.0
    assert age.numeric_max == 200.0

    notes = p.column("notes")
    assert notes is not None
    assert notes.null_count == 3
    assert notes.null_pct == 0.75


def test_empty_csv_profile(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "empty.csv")

    assert p.profile_status is ProfileStatus.OK
    assert p.row_count == 0
    assert p.column_count == 3
    # No division-by-zero: empty file reports 0.0 null_pct, not NaN.
    assert all(c.null_pct == 0.0 for c in p.columns)
    assert all(c.distinct_count == 0 for c in p.columns)


def test_clean_json_profile(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "clean.json")

    assert p.profile_status is ProfileStatus.OK
    assert p.file_format == "json"
    assert p.row_count == 3
    assert p.column_count == 3


def test_dirty_parquet_profile(dirty_parquet: Path) -> None:
    p = profile_file(dirty_parquet)

    assert p.profile_status is ProfileStatus.OK
    assert p.file_format == "parquet"
    assert p.row_count == 4

    idc = p.column("id")
    assert idc is not None
    assert idc.distinct_count == 3

    age = p.column("age")
    assert age is not None
    assert age.numeric_min == -5.0
    assert age.numeric_max == 200.0


def test_corrupt_parquet_is_unparseable(fixtures_dir: Path) -> None:
    p = profile_file(fixtures_dir / "corrupt.parquet")

    assert p.profile_status is ProfileStatus.UNPARSEABLE
    assert p.error is not None
    assert p.columns == []
    assert p.row_count == 0


def test_sample_rows_are_json_serializable(fixtures_dir: Path) -> None:
    # clean.csv has DATE and DECIMAL columns; sample rows must still serialize.
    p = profile_file(fixtures_dir / "clean.csv")
    assert len(p.sample_rows) > 0
    json.dumps(p.sample_rows)  # must not raise


def test_format_override_beats_extension(fixtures_dir: Path) -> None:
    # Explicit file_format wins over the extension.
    p = profile_file(fixtures_dir / "clean.csv", file_format="csv")
    assert p.file_format == "csv"
    assert p.row_count == 5
