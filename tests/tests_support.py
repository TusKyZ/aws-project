"""Small helpers shared by handler tests."""

from __future__ import annotations


class FakeContext:
    """Just enough Lambda context for Powertools' inject_lambda_context."""

    function_name = "sentinel-test"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:sentinel-test"
    aws_request_id = "00000000-0000-0000-0000-000000000000"
