"""Phase 0 sanity check: proves pytest, imports, and CI wiring work end to end.

Replaced by real tests from Phase 1 onward.
"""

import sys


def test_python_version_is_313() -> None:
    # Matches the Lambda runtime (python3.13) so dev == prod.
    assert sys.version_info[:2] == (3, 13)


def test_core_dependencies_importable() -> None:
    import anthropic  # noqa: F401
    import boto3  # noqa: F401
    import duckdb  # noqa: F401
    import pydantic  # noqa: F401
