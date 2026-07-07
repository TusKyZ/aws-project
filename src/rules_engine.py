"""Deterministic data-quality rules — the free first pass before the LLM.

Each check is a pure function over a `FileProfile` (plus config / a previous
profile for drift), returning typed `RuleFinding`s. Rules catch what can be
pre-written — null bursts, duplicate keys, range violations, empty files, schema
drift — leaving novel logical anomalies to the LLM in Phase 2. Findings feed both
the audit record and the LLM prompt, so Claude builds on them rather than
rediscovering them.

The schema-drift check is a pure profile-vs-profile comparison; fetching the
previous profile from DynamoDB is wired in Phase 5.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from models import FileProfile, RuleFinding, SemanticType

_DEFAULT_KEY_PATTERNS = ("id", "uuid", "guid", "key", "pk", "code")


class RangeRule(BaseModel):
    """Allowed numeric range for a named column. None bound = unbounded that side."""

    column: str
    min: float | None = None
    max: float | None = None


class RulesConfig(BaseModel):
    null_pct_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    key_column_patterns: tuple[str, ...] = _DEFAULT_KEY_PATTERNS
    range_rules: list[RangeRule] = Field(
        default_factory=lambda: [RangeRule(column="age", min=0, max=150)]
    )


def _is_key_candidate(name: str, patterns: tuple[str, ...]) -> bool:
    n = name.lower()
    for p in patterns:
        if n == p or n.endswith(f"_{p}") or n.startswith(f"{p}_"):
            return True
    return False


def check_empty_file(profile: FileProfile) -> list[RuleFinding]:
    if profile.row_count == 0:
        return [
            RuleFinding(
                rule_id="empty_file",
                severity="high",
                message="File contains a header/schema but zero data rows.",
                details={"row_count": 0},
            )
        ]
    return []


def check_null_threshold(profile: FileProfile, config: RulesConfig) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    for col in profile.columns:
        if col.null_pct >= config.null_pct_threshold and col.null_pct > 0.0:
            findings.append(
                RuleFinding(
                    rule_id="null_threshold",
                    severity="high" if col.null_pct >= 0.5 else "medium",
                    message=f"Column '{col.name}' is {col.null_pct:.0%} null.",
                    column=col.name,
                    details={"null_pct": col.null_pct, "threshold": config.null_pct_threshold},
                )
            )
    return findings


def check_duplicate_keys(profile: FileProfile, config: RulesConfig) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    for col in profile.columns:
        if not _is_key_candidate(col.name, config.key_column_patterns):
            continue
        non_null = profile.row_count - col.null_count
        if non_null > col.distinct_count:
            findings.append(
                RuleFinding(
                    rule_id="duplicate_key",
                    severity="high",
                    message=(
                        f"Key-like column '{col.name}' has duplicates "
                        f"({non_null} non-null values, {col.distinct_count} distinct)."
                    ),
                    column=col.name,
                    details={"non_null": non_null, "distinct": col.distinct_count},
                )
            )
    return findings


def check_range_violations(profile: FileProfile, config: RulesConfig) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    for rule in config.range_rules:
        col = profile.column(rule.column)
        if col is None or col.semantic_type is not SemanticType.NUMERIC:
            continue
        if col.numeric_min is None or col.numeric_max is None:
            continue
        below = rule.min is not None and col.numeric_min < rule.min
        above = rule.max is not None and col.numeric_max > rule.max
        if below or above:
            findings.append(
                RuleFinding(
                    rule_id="range_violation",
                    severity="high",
                    message=(
                        f"Column '{col.name}' has values outside the allowed range "
                        f"[{rule.min}, {rule.max}]."
                    ),
                    column=col.name,
                    details={
                        "observed_min": col.numeric_min,
                        "observed_max": col.numeric_max,
                        "allowed_min": rule.min,
                        "allowed_max": rule.max,
                        "below_min": below,
                        "above_max": above,
                    },
                )
            )
    return findings


def check_schema_drift(
    current: FileProfile, previous: FileProfile | None
) -> list[RuleFinding]:
    """Compare the current profile against the last-seen one. Pure function; the
    Phase 5 wiring fetches `previous` from the `LATEST#<dataset>` DynamoDB item.
    """
    if previous is None:
        return []

    cur_types = {c.name: c.duckdb_type for c in current.columns}
    prev_types = {c.name: c.duckdb_type for c in previous.columns}

    added = sorted(set(cur_types) - set(prev_types))
    removed = sorted(set(prev_types) - set(cur_types))
    retyped = {
        name: {"from": prev_types[name], "to": cur_types[name]}
        for name in sorted(set(cur_types) & set(prev_types))
        if cur_types[name] != prev_types[name]
    }

    if not (added or removed or retyped):
        return []
    return [
        RuleFinding(
            rule_id="schema_drift",
            severity="medium",
            message="Schema changed versus the last-seen profile for this dataset.",
            details={"added": added, "removed": removed, "retyped": retyped},
        )
    ]


_SEVERITY_PENALTY = {"high": 25, "medium": 10, "low": 5}


def fallback_score(findings: list[RuleFinding]) -> int:
    """Deterministic score when the LLM layer is skipped or down.

    100 minus a per-finding severity penalty, floored at 5 (0 is reserved for
    unparseable files). Keeps the audit record's score meaningful during
    graceful degradation instead of defaulting to a lie like 100.
    """
    score = 100 - sum(_SEVERITY_PENALTY[f.severity] for f in findings)
    return max(score, 5)


def run_rules(
    profile: FileProfile,
    config: RulesConfig | None = None,
    previous: FileProfile | None = None,
) -> list[RuleFinding]:
    """Run every deterministic check and return the combined findings."""
    config = config or RulesConfig()
    findings: list[RuleFinding] = []
    findings += check_empty_file(profile)
    findings += check_null_threshold(profile, config)
    findings += check_duplicate_keys(profile, config)
    findings += check_range_violations(profile, config)
    findings += check_schema_drift(profile, previous)
    return findings
