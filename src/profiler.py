"""DuckDB statistical profiling.

One pass over a file produces a `FileProfile`: row count, per-column null
counts, exact distinct counts, min/max, and a small sample of rows. Reads local
paths now; the Phase 4 change is swapping the path for an ``s3://`` URI (DuckDB's
httpfs reads it the same way).

The file path is passed to DuckDB as a bound parameter (`?`), never string-
interpolated, so an attacker-controlled S3 key cannot inject SQL. Column
identifiers come from DuckDB's own DESCRIBE output and are still quote-escaped
defensively.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import os
from pathlib import Path
from typing import Any

import duckdb

from models import ColumnProfile, FileProfile, ProfileStatus, SemanticType

_READERS = {
    "csv": "read_csv_auto",
    "parquet": "read_parquet",
    "json": "read_json_auto",
}

_NUMERIC_PREFIXES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "DECIMAL", "NUMERIC", "FLOAT", "REAL", "DOUBLE",
)
_TEMPORAL_PREFIXES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
_COMPLEX_PREFIXES = ("STRUCT", "LIST", "MAP", "ARRAY", "UNION")

_SAMPLE_ROW_LIMIT = 5


def _infer_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in _READERS:
        raise ValueError(f"Unsupported file extension: {path.suffix!r} (expected csv/parquet/json)")
    return suffix


def _infer_format_from_key(key: str) -> str:
    """Format from an S3 key/URI suffix (Path would mangle `s3://`)."""
    _, dot, suffix = key.rpartition(".")
    suffix = suffix.lower()
    if not dot or suffix not in _READERS:
        raise ValueError(f"Unsupported file extension on key: {key!r} (expected csv/parquet/json)")
    return suffix


def _configure_s3(con: duckdb.DuckDBPyConnection) -> None:
    """Prepare a connection for s3:// reads inside Lambda.

    The layer bundles the httpfs extension, so LOAD succeeds offline; INSTALL
    is only a local-dev fallback. Lambda's filesystem is read-only outside
    /tmp, hence the home_directory override. Credentials come from the
    execution role via DuckDB's credential_chain provider — no key material.
    """
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        con.execute("SET home_directory='/tmp'")  # noqa: S108 - Lambda's only writable path
    try:
        con.execute("LOAD httpfs")
    except duckdb.Error:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    con.execute("CREATE OR REPLACE SECRET sentinel_s3 (TYPE s3, PROVIDER credential_chain)")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _classify(duckdb_type: str) -> SemanticType:
    upper = duckdb_type.upper()
    if upper.startswith(_COMPLEX_PREFIXES):
        return SemanticType.OTHER
    if upper.startswith(_NUMERIC_PREFIXES):
        return SemanticType.NUMERIC
    if upper.startswith(_TEMPORAL_PREFIXES):
        return SemanticType.TEMPORAL
    if upper.startswith("BOOLEAN"):
        return SemanticType.BOOLEAN
    return SemanticType.STRING


def _is_simple(duckdb_type: str) -> bool:
    """Min/max/distinct aggregates are only valid on scalar (non-nested) columns."""
    return not duckdb_type.upper().startswith(_COMPLEX_PREFIXES)


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time, decimal.Decimal)):
        return str(value)
    return str(value)


def _to_str(value: Any) -> str | None:
    return None if value is None else str(value)


def profile_file(
    path: str | Path,
    *,
    source_uri: str | None = None,
    file_format: str | None = None,
) -> FileProfile:
    """Profile a single data file. Never raises on bad data — an unreadable file
    yields a `FileProfile` with `profile_status=UNPARSEABLE`, which downstream
    treats as a data-quality finding (score 0), not an infrastructure error.
    """
    remote = isinstance(path, str) and path.startswith("s3://")
    if remote:
        fmt = file_format or _infer_format_from_key(path)
        duck_path = path
        uri = source_uri or path
    else:
        path = Path(path)
        fmt = file_format or _infer_format(path)
        uri = source_uri or str(path)
        # DuckDB's S3 paths use forward slashes; as_posix keeps local Windows
        # paths valid too.
        duck_path = path.as_posix()
    reader = _READERS[fmt]

    # SQL is built with an allowlisted `reader`, quote-escaped column identifiers,
    # and the path bound as a parameter (never interpolated) — so the S608
    # string-built-query warnings below are false positives, suppressed per line.
    con = duckdb.connect()
    try:
        if remote:
            _configure_s3(con)
        describe = con.execute(
            f"DESCRIBE SELECT * FROM {reader}(?)", [duck_path]  # noqa: S608
        ).fetchall()
        cols: list[tuple[str, str]] = [(r[0], r[1]) for r in describe]

        select_parts = ["count(*) AS __rowcount"]
        for i, (name, dtype) in enumerate(cols):
            q = _quote_ident(name)
            select_parts.append(f"count({q}) AS nn_{i}")
            if _is_simple(dtype):
                select_parts.append(f"min({q}) AS mn_{i}")
                select_parts.append(f"max({q}) AS mx_{i}")
                select_parts.append(f"count(DISTINCT {q}) AS dc_{i}")
        agg_sql = f"SELECT {', '.join(select_parts)} FROM {reader}(?)"  # noqa: S608
        agg_cursor = con.execute(agg_sql, [duck_path])
        agg_cols = [d[0] for d in agg_cursor.description]
        agg = dict(zip(agg_cols, agg_cursor.fetchone(), strict=True))

        sample_cursor = con.execute(
            f"SELECT * FROM {reader}(?) LIMIT {_SAMPLE_ROW_LIMIT}", [duck_path]  # noqa: S608
        )
        sample_cols = [d[0] for d in sample_cursor.description]
        sample_rows = [
            {col: _to_json_safe(val) for col, val in zip(sample_cols, row, strict=True)}
            for row in sample_cursor.fetchall()
        ]
    except duckdb.Error as exc:
        return FileProfile(
            source_uri=uri,
            file_format=fmt,  # type: ignore[arg-type]
            profile_status=ProfileStatus.UNPARSEABLE,
            error=str(exc),
        )
    finally:
        con.close()

    row_count: int = agg["__rowcount"]
    columns: list[ColumnProfile] = []
    for i, (name, dtype) in enumerate(cols):
        non_null = agg[f"nn_{i}"]
        null_count = row_count - non_null
        null_pct = (null_count / row_count) if row_count else 0.0
        simple = _is_simple(dtype)
        mn = agg.get(f"mn_{i}") if simple else None
        mx = agg.get(f"mx_{i}") if simple else None
        distinct = (agg.get(f"dc_{i}") or 0) if simple else 0
        semantic = _classify(dtype)
        numeric_min = float(mn) if (semantic is SemanticType.NUMERIC and mn is not None) else None
        numeric_max = float(mx) if (semantic is SemanticType.NUMERIC and mx is not None) else None
        columns.append(
            ColumnProfile(
                name=name,
                duckdb_type=dtype,
                semantic_type=semantic,
                null_count=null_count,
                null_pct=null_pct,
                distinct_count=distinct,
                min_value=_to_str(mn),
                max_value=_to_str(mx),
                numeric_min=numeric_min,
                numeric_max=numeric_max,
            )
        )

    return FileProfile(
        source_uri=uri,
        file_format=fmt,  # type: ignore[arg-type]
        profile_status=ProfileStatus.OK,
        row_count=row_count,
        column_count=len(cols),
        columns=columns,
        sample_rows=sample_rows,
    )
