"""Forge: research — failure classification and retry advisory (D011).

Two-layer research:
1. Deterministic classifier: extracts failure_kind + failure_facts from build/verify logs.
2. LLM advisory (optional): proposes BUILD_PACKET diff based on classification.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from kavi.artifacts.writer import write_artifact
from kavi.ledger.models import (
    Artifact,
    ArtifactKind,
    Build,
    BuildStatus,
    Verification,
    VerificationStatus,
    get_artifacts_for_related,
    get_build,
    get_builds_for_proposal,
    get_latest_verification,
)

# ---------------------------------------------------------------------------
# Failure classification (layer 1 — deterministic)
# ---------------------------------------------------------------------------


class FailureKind(StrEnum):
    GATE_VIOLATION = "GATE_VIOLATION"
    TIMEOUT = "TIMEOUT"
    BUILD_ERROR = "BUILD_ERROR"
    VERIFY_LINT = "VERIFY_LINT"
    VERIFY_TEST = "VERIFY_TEST"
    VERIFY_POLICY = "VERIFY_POLICY"
    VERIFY_INVARIANT = "VERIFY_INVARIANT"
    UNKNOWN = "UNKNOWN"


_LOG_EXCERPT_MAX = 2000


@dataclass
class FailureAnalysis:
    kind: FailureKind
    facts: list[str] = field(default_factory=list)
    log_excerpt: str = ""
    attempt_number: int = 1
    build_id: str = ""


def _extract_excerpt(text: str, max_len: int = _LOG_EXCERPT_MAX) -> str:
    """Return a bounded excerpt of text."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n... (truncated)"


def classify_failure(
    build: Build,
    build_log: str,
    verification: Verification | None = None,
) -> FailureAnalysis:
    """Classify a build/verify failure from logs and records.

    Deterministic — no LLM calls. Fully testable.
    """
    facts: list[str] = []

    # Check verification failures first (more specific)
    if verification is not None and verification.status == VerificationStatus.FAILED:
        if not verification.invariant_ok:
            facts.append("Invariant check failed")
            return FailureAnalysis(
                kind=FailureKind.VERIFY_INVARIANT,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )
        if not verification.policy_ok:
            facts.append("Policy scanner found violations")
            return FailureAnalysis(
                kind=FailureKind.VERIFY_POLICY,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )
        if not verification.pytest_ok:
            facts.append("pytest failed")
            return FailureAnalysis(
                kind=FailureKind.VERIFY_TEST,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )
        if not verification.ruff_ok or not verification.mypy_ok:
            if not verification.ruff_ok:
                facts.append("ruff check failed")
            if not verification.mypy_ok:
                facts.append("mypy check failed")
            return FailureAnalysis(
                kind=FailureKind.VERIFY_LINT,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )

    # Build-level failures
    if build.status == BuildStatus.FAILED:
        summary = build.summary or ""

        # Timeout
        if "Timeout" in summary or "TIMEOUT" in build_log[:500]:
            facts.append(f"Build timed out: {summary}")
            return FailureAnalysis(
                kind=FailureKind.TIMEOUT,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )

        # Diff gate violation
        if "Diff gate" in summary or "gate failed" in summary.lower():
            # Extract violation details from build log
            violations_match = re.search(
                r"Violations:\s*\[([^\]]*)\]", build_log
            )
            if violations_match:
                facts.append(f"Disallowed files: {violations_match.group(1)}")
            missing_match = re.search(
                r"Required missing:\s*\[([^\]]*)\]", build_log
            )
            if missing_match:
                facts.append(f"Missing files: {missing_match.group(1)}")
            facts.append(f"Gate summary: {summary}")
            return FailureAnalysis(
                kind=FailureKind.GATE_VIOLATION,
                facts=facts,
                log_excerpt=_extract_excerpt(build_log),
                attempt_number=build.attempt_number,
                build_id=build.id,
            )

        # Generic build error (non-zero exit, claude not found, etc.)
        facts.append(f"Build failed: {summary}")
        exit_match = re.search(r"Exit code:\s*(\d+)", build_log)
        if exit_match:
            facts.append(f"Exit code: {exit_match.group(1)}")
        return FailureAnalysis(
            kind=FailureKind.BUILD_ERROR,
            facts=facts,
            log_excerpt=_extract_excerpt(build_log),
            attempt_number=build.attempt_number,
            build_id=build.id,
        )

    return FailureAnalysis(
        kind=FailureKind.UNKNOWN,
        facts=["Could not determine failure cause"],
        log_excerpt=_extract_excerpt(build_log),
        attempt_number=build.attempt_number,
        build_id=build.id,
    )


def _find_build_log(
    conn: sqlite3.Connection, build: Build
) -> str:
    """Find and read the build log artifact for a build."""
    artifacts = get_artifacts_for_related(conn, build.proposal_id)
    for art in reversed(artifacts):
        if art.kind == ArtifactKind.BUILD_LOG and build.id in art.path:
            p = Path(art.path)
            if p.exists():
                return p.read_text(encoding="utf-8")
    return ""


def research_skill(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    output_dir: Path,
    user_hint: str | None = None,
) -> tuple[FailureAnalysis, Artifact]:
    """Analyze a failed build and produce a RESEARCH_NOTE artifact.

    Validates build is FAILED (or associated verification is FAILED).
    Returns (FailureAnalysis, Artifact).
    """
    build = get_build(conn, build_id)
    if build is None:
        raise ValueError(f"Build '{build_id}' not found")

    # Allow research on failed builds or builds whose verification failed
    verification = get_latest_verification(conn, build.proposal_id)
    if build.status != BuildStatus.FAILED and (
        verification is None or verification.status != VerificationStatus.FAILED
    ):
        raise ValueError(
            f"Build '{build_id}' is not failed (status={build.status}) "
            "and has no failed verification"
        )

    build_log = _find_build_log(conn, build)
    analysis = classify_failure(build, build_log, verification)

    # Format research note content
    lines = [
        f"# Research Note: Build {build_id}\n",
        f"## Failure Classification: {analysis.kind.value}\n",
        f"**Attempt:** {analysis.attempt_number}",
        f"**Build ID:** {analysis.build_id}\n",
        "## Facts",
    ]
    for fact in analysis.facts:
        lines.append(f"- {fact}")

    if user_hint:
        lines.append(f"\n## User Hint\n{user_hint}")

    if analysis.log_excerpt:
        lines.append("\n## Log Excerpt")
        lines.append(f"```\n{analysis.log_excerpt}\n```")

    content = "\n".join(lines) + "\n"

    artifact = write_artifact(
        conn,
        content=content,
        path=output_dir / f"research_{build_id}.md",
        kind=ArtifactKind.RESEARCH_NOTE,
        related_id=build.proposal_id,
    )

    return analysis, artifact


# ---------------------------------------------------------------------------
# LLM advisory (layer 2 — optional)
# ---------------------------------------------------------------------------


class EscalationTrigger(StrEnum):
    REPEATED_FAILURE = "REPEATED_FAILURE"
    PERMISSION_WIDENING = "PERMISSION_WIDENING"
    SECURITY_CLASS = "SECURITY_CLASS"
    LARGE_DIFF = "LARGE_DIFF"
    AMBIGUOUS = "AMBIGUOUS"


def _check_escalation_triggers(
    conn: sqlite3.Connection,
    *,
    analysis: FailureAnalysis,
    original_packet: str,
    proposed_packet: str,
) -> list[EscalationTrigger]:
    """Check if any escalation triggers fire."""
    triggers: list[EscalationTrigger] = []

    # Repeated same failure kind (>= 3 consecutive)
    build = get_build(conn, analysis.build_id)
    if build is not None:
        prior_builds = get_builds_for_proposal(conn, build.proposal_id)
        failed_builds = [b for b in prior_builds if b.status == BuildStatus.FAILED]
        if len(failed_builds) >= 3:
            # Check if the last 3 all have RESEARCH_NOTEs with same kind
            # (simplified: just check count of consecutive failures)
            triggers.append(EscalationTrigger.REPEATED_FAILURE)

    # Security-class failure
    if analysis.kind in (FailureKind.VERIFY_POLICY, FailureKind.VERIFY_INVARIANT):
        triggers.append(EscalationTrigger.SECURITY_CLASS)

    # Permission widening — check for wider side_effect_class or new secrets
    orig_lower = original_packet.lower()
    prop_lower = proposed_packet.lower()
    escalating_keywords = ["network", "money", "messaging", "secret"]
    for kw in escalating_keywords:
        if kw in prop_lower and kw not in orig_lower:
            triggers.append(EscalationTrigger.PERMISSION_WIDENING)
            break

    # Large diff
    orig_lines = original_packet.splitlines()
    prop_lines = proposed_packet.splitlines()
    if orig_lines:
        changed = sum(1 for a, b in zip(orig_lines, prop_lines) if a != b)
        added = abs(len(prop_lines) - len(orig_lines))
        diff_ratio = (changed + added) / max(len(orig_lines), 1)
        if diff_ratio > 0.5:
            triggers.append(EscalationTrigger.LARGE_DIFF)

    # Ambiguity
    if analysis.kind == FailureKind.UNKNOWN:
        triggers.append(EscalationTrigger.AMBIGUOUS)

    return triggers


def advise_retry(
    conn: sqlite3.Connection,
    *,
    analysis: FailureAnalysis,
    original_packet: str,
    output_dir: Path,
    auto: bool = True,
) -> tuple[str, list[EscalationTrigger]]:
    """LLM-advised BUILD_PACKET diff for retry.

    Returns (proposed_packet_content, escalation_triggers).
    If escalation_triggers is non-empty, human review required.
    """
    from openai import OpenAI

    client = OpenAI(api_key="dummy-key", base_url="http://localhost:8000/v1")

    prompt = f"""You are a build system assistant. A skill build attempt failed.

## Failure Classification
- **Kind:** {analysis.kind.value}
- **Attempt:** {analysis.attempt_number}

## Facts
{chr(10).join(f'- {f}' for f in analysis.facts)}

## Log Excerpt
```
{analysis.log_excerpt[:1500]}
```

## Original BUILD_PACKET
```markdown
{original_packet}
```

## Task
Propose a corrected BUILD_PACKET that addresses the failure. Output ONLY the corrected
BUILD_PACKET content (markdown), nothing else. Keep the same structure but fix the
instructions to avoid the failure. Do NOT widen permissions, add secrets, or change
the side effect class."""

    response = client.chat.completions.create(
        model="gpt-oss-20b",
        messages=[{"role": "user", "content": prompt}],
    )
    proposed = response.choices[0].message.content or original_packet

    triggers = _check_escalation_triggers(
        conn,
        analysis=analysis,
        original_packet=original_packet,
        proposed_packet=proposed,
    )

    return proposed, triggers
