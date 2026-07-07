"""Shared test fixtures.

Text fixtures (CSV/JSON) live committed under `tests/fixtures/`. The Parquet
fixture is generated at runtime from the committed `dirty.csv` so the binary
never enters git while the `read_parquet` code path is still exercised.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture()
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake AWS credentials/region so moto never touches a real account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(scope="session")
def dirty_parquet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Parquet built from the same rows as dirty.csv (same expected findings)."""
    out = tmp_path_factory.mktemp("parquet") / "dirty.parquet"
    src = (FIXTURES / "dirty.csv").as_posix()
    con = duckdb.connect()
    # COPY ... TO does not accept a bound parameter for the target; these paths
    # are trusted test fixtures, so direct interpolation is safe here.
    copy_sql = (
        f"COPY (SELECT * FROM read_csv_auto('{src}')) "
        f"TO '{out.as_posix()}' (FORMAT PARQUET)"
    )
    con.execute(copy_sql)
    con.close()
    return out
