"""Forge: build-skill — generate code via Claude Code in a sandbox (D009)."""

from __future__ import annotations

import hashlib
import posixpath
import shutil
import sqlite3
import subprocess
from pathlib import Path

from kavi.artifacts.writer import write_artifact, write_build_packet
from kavi.forge.paths import skill_file_path, skill_test_path
from kavi.ledger.models import (
    Artifact,
    ArtifactKind,
    Build,
    BuildStatus,
    ProposalStatus,
    get_proposal,
    insert_build,
    update_build,
    update_proposal_status,
)

# ---------------------------------------------------------------------------
# Build packet generation
# ---------------------------------------------------------------------------

# Patterns excluded from sandbox copies (matched by rglob)
_SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "credentials.json"]


def _create_build_packet_content(
    name: str,
    description: str,
    io_schema: str,
    side_effect_class: str,
) -> str:
    """Generate the BUILD_PACKET.md content for Claude Code."""
    return f"""# Build Packet: {name}

## Task
Generate a Kavi skill implementation for "{name}".

## Skill Specification
- **Name**: {name}
- **Description**: {description}
- **Side Effect Class**: {side_effect_class}

## I/O Schema
```json
{io_schema}
```

## Requirements
1. Create `src/kavi/skills/{name}.py` implementing `BaseSkill`
2. The skill class must define: name, description, input_model, output_model, side_effect_class
3. Implement the `execute()` method
4. Use Pydantic models for input/output validation
5. Do NOT use any forbidden imports (subprocess, os.system, eval, exec)
6. Only write to allowed paths: ./vault_out/, ./artifacts_out/

## File Structure
```
src/kavi/skills/{name}.py  — skill implementation
tests/test_skill_{name}.py — unit tests
```

## Constraints
- ONLY create the two files listed above.
- Do NOT modify any other files.
- Do NOT run, commit, or push anything.
"""


def build_skill(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    output_dir: Path,
    branch_name: str | None = None,
) -> tuple[Build, Artifact]:
    """Start a skill build: record build, write build packet.

    Returns (Build, Artifact) for the build packet.
    The actual Claude Code invocation is a separate step (invoke_claude_build).
    """
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    if proposal.status != ProposalStatus.PROPOSED:
        raise ValueError(
            f"Proposal '{proposal_id}' has status {proposal.status}, expected PROPOSED"
        )

    if branch_name is None:
        branch_name = f"skill/{proposal.name}-{proposal.id[:8]}"

    build = Build(
        proposal_id=proposal_id,
        branch_name=branch_name,
    )
    insert_build(conn, build)

    content = _create_build_packet_content(
        name=proposal.name,
        description=proposal.description,
        io_schema=proposal.io_schema_json,
        side_effect_class=proposal.side_effect_class.value,
    )
    artifact = write_build_packet(
        conn, content=content, proposal_id=proposal_id, output_dir=output_dir,
    )

    return build, artifact


# ---------------------------------------------------------------------------
# Sandbox workspace (D009) — working-tree copy + fresh git baseline
# ---------------------------------------------------------------------------


def _is_secret_file(name: str) -> bool:
    """Check if a filename matches any secret pattern."""
    from fnmatch import fnmatch
    return any(fnmatch(name, pat) for pat in _SECRET_PATTERNS)


def create_sandbox(project_root: Path, sandbox_parent: Path) -> Path:
    """Create an isolated workspace for building.

    Copies the working tree (excluding .git/ and secret files),
    then initializes a fresh git repo with a baseline commit.
    Zero hooks, zero remotes, zero config risk.
    """
    sandbox = sandbox_parent / "repo"

    def _ignore(directory: str, entries: list[str]) -> set[str]:
        ignored: set[str] = set()
        for entry in entries:
            # Always exclude .git directory
            if entry == ".git":
                ignored.add(entry)
                continue
            # Exclude secret files
            if _is_secret_file(entry):
                ignored.add(entry)
        return ignored

    shutil.copytree(project_root, sandbox, symlinks=False, ignore=_ignore)

    # Create a fresh git repo with a known baseline
    subprocess.run(  # noqa: S603
        ["git", "init"],
        cwd=str(sandbox), capture_output=True, check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "config", "user.email", "kavi@local"],
        cwd=str(sandbox), capture_output=True, check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "config", "user.name", "kavi"],
        cwd=str(sandbox), capture_output=True, check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "add", "-A"],
        cwd=str(sandbox), capture_output=True, check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "commit", "-m", "sandbox baseline"],
        cwd=str(sandbox), capture_output=True, check=True,
    )

    return sandbox


# ---------------------------------------------------------------------------
# Diff allowlist gate (D009)
# ---------------------------------------------------------------------------


class DiffGateResult:
    """Structured result from the diff allowlist gate."""

    __slots__ = (
        "ok", "changed_tracked", "changed_untracked",
        "allowed", "violations", "required_missing",
    )

    def __init__(
        self,
        *,
        ok: bool,
        changed_tracked: list[str],
        changed_untracked: list[str],
        allowed: list[str],
        violations: list[str],
        required_missing: list[str],
    ) -> None:
        self.ok = ok
        self.changed_tracked = changed_tracked
        self.changed_untracked = changed_untracked
        self.allowed = allowed
        self.violations = violations
        self.required_missing = required_missing


def diff_allowlist_gate(
    proposal_name: str,
    sandbox_root: Path,
) -> DiffGateResult:
    """Check that only allowlisted files were changed in the sandbox.

    Compares against the sandbox baseline commit. Detects modifications,
    additions, deletions, and renames.
    """
    # Modified/deleted tracked files (against baseline)
    tracked_result = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(sandbox_root),
        capture_output=True, text=True, check=False,
    )
    # New untracked files (respecting .gitignore)
    untracked_result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(sandbox_root),
        capture_output=True, text=True, check=False,
    )

    changed_tracked = [
        line.strip() for line in tracked_result.stdout.strip().splitlines()
        if line.strip()
    ]
    changed_untracked = [
        line.strip() for line in untracked_result.stdout.strip().splitlines()
        if line.strip()
    ]
    all_changed = set(changed_tracked) | set(changed_untracked)

    if not all_changed:
        return DiffGateResult(
            ok=False,
            changed_tracked=[], changed_untracked=[],
            allowed=[], violations=["No files were created or modified"],
            required_missing=[],
        )

    # Allowed paths (relative to repo root, posix form)
    skill_rel = str(skill_file_path(proposal_name, Path(".")).as_posix())
    test_rel = str(skill_test_path(proposal_name, Path(".")).as_posix())
    allowed_set = {skill_rel, test_rel}

    allowed = sorted(all_changed & allowed_set)
    violations = sorted(all_changed - allowed_set)
    required_missing = sorted(allowed_set - all_changed)

    ok = len(required_missing) == 0 and len(violations) == 0

    return DiffGateResult(
        ok=ok,
        changed_tracked=changed_tracked,
        changed_untracked=changed_untracked,
        allowed=allowed,
        violations=violations,
        required_missing=required_missing,
    )


# ---------------------------------------------------------------------------
# Copy-back with safety checks (D009)
# ---------------------------------------------------------------------------


def _safe_copy_back(
    sandbox: Path,
    project_root: Path,
    allowed_changes: list[str],
) -> list[str]:
    """Copy allowlisted files from sandbox to canonical repo.

    Safety checks:
    - Rejects relative paths containing '..' or absolute paths
    - Rejects symlinks in source
    - Validates resolved destination is under project_root
    - Returns list of (rel_path, overwritten) tuples as log lines
    """
    repo_root_resolved = project_root.resolve()
    copied: list[str] = []

    for rel_path in allowed_changes:
        # Normalize and reject traversal / absolute paths
        normalized = posixpath.normpath(rel_path)
        if normalized.startswith("/") or normalized.startswith(".."):
            raise ValueError(
                f"Refusing path with traversal or absolute: {rel_path!r} "
                f"(normalized: {normalized!r})"
            )
        if normalized != rel_path:
            raise ValueError(
                f"Path is not normalized: {rel_path!r} "
                f"(expected: {normalized!r})"
            )

        src = sandbox / rel_path
        dst = project_root / rel_path

        # Reject symlinks — Claude could point them outside the sandbox
        if src.is_symlink():
            raise ValueError(
                f"Refusing to copy symlink: {rel_path} -> {src.readlink()}"
            )

        # Validate destination resolves under project root
        dst_resolved = dst.resolve()
        if not str(dst_resolved).startswith(str(repo_root_resolved) + "/"):
            raise ValueError(
                f"Path traversal detected: {rel_path} resolves to "
                f"{dst_resolved}, outside {repo_root_resolved}"
            )

        overwritten = dst.exists()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        action = "overwrite" if overwritten else "create"
        copied.append(f"{rel_path} ({action})")

    return copied


# ---------------------------------------------------------------------------
# Claude Code invocation (D009: headless, sandboxed)
# ---------------------------------------------------------------------------

# Tools Claude Code is allowed to use during build.
# Bash is intentionally excluded — no arbitrary shell at build time.
ALLOWED_TOOLS = ["Edit", "Write", "Glob", "Grep", "Read"]

_STDOUT_MAX = 50_000
_STDERR_MAX = 10_000


def _build_log_content(
    *,
    proposal_name: str,
    build_id: str,
    proposal_id: str,
    packet_sha256: str,
    sandbox_path: Path,
    cmd: list[str],
    allowed_tools: list[str],
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    gate: DiffGateResult | None = None,
    timeout: bool = False,
    timeout_secs: int = 0,
) -> str:
    """Format a complete build log with all audit fields."""
    lines = [
        f"# Build Log: {proposal_name}\n",
        "## Metadata",
        f"- **Build ID**: `{build_id}`",
        f"- **Proposal ID**: `{proposal_id}`",
        f"- **Packet SHA256**: `{packet_sha256}`",
        f"- **Sandbox**: `{sandbox_path}`",
        f"- **Command**: `{' '.join(cmd)}`",
        f"- **Allowed tools**: {', '.join(allowed_tools)}\n",
    ]

    if timeout:
        lines.append(f"## Result: TIMEOUT after {timeout_secs}s\n")
        return "\n".join(lines)

    lines.append(f"## Exit code: {exit_code}\n")

    stdout_truncated = len(stdout) > _STDOUT_MAX
    stderr_truncated = len(stderr) > _STDERR_MAX
    lines.append(
        f"## stdout ({len(stdout)} bytes"
        f"{', truncated' if stdout_truncated else ''})"
    )
    lines.append(f"```\n{stdout[:_STDOUT_MAX]}\n```\n")
    lines.append(
        f"## stderr ({len(stderr)} bytes"
        f"{', truncated' if stderr_truncated else ''})"
    )
    lines.append(f"```\n{stderr[:_STDERR_MAX]}\n```\n")

    if gate is not None:
        verdict = "PASS" if gate.ok else "FAIL"
        lines.append(f"## Diff Allowlist Gate: {verdict}")
        lines.append(f"- Changed (tracked): {gate.changed_tracked}")
        lines.append(f"- Changed (untracked): {gate.changed_untracked}")
        lines.append(f"- Allowed: {gate.allowed}")
        if gate.violations:
            lines.append(f"- Violations: {gate.violations}")
        if gate.required_missing:
            lines.append(f"- Required missing: {gate.required_missing}")
        lines.append("")

    return "\n".join(lines)


def invoke_claude_build(
    conn: sqlite3.Connection,
    *,
    build: Build,
    proposal_name: str,
    build_packet_path: Path,
    project_root: Path,
    output_dir: Path,
    timeout: int = 600,
) -> tuple[bool, Path | None]:
    """Run Claude Code headlessly in a sandbox to generate skill files.

    Flow (D009):
    1. Create sandbox workspace (working-tree copy + fresh git baseline)
    2. Invoke `claude -p` with --allowedTools in sandbox
    3. Run diff_allowlist_gate() against sandbox baseline
    4. If gate passes, safe-copy allowlisted files to canonical repo
    5. Record build log artifact, mark build succeeded/failed

    Returns (success, sandbox_path).
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        mark_build_failed(conn, build.id, summary="claude CLI not found on PATH")
        return False, None

    # (a) Create sandbox workspace
    sandbox_parent = Path("/tmp") / "kavi-build" / build.id  # noqa: S108
    sandbox_parent.mkdir(parents=True, exist_ok=True)
    sandbox = create_sandbox(project_root, sandbox_parent)

    # (b) Headless Claude Code invocation
    packet_content = build_packet_path.read_text(encoding="utf-8")
    packet_sha256 = hashlib.sha256(packet_content.encode()).hexdigest()

    cmd = [claude_bin, "-p", "--output-format", "text"]
    for tool in ALLOWED_TOOLS:
        cmd.extend(["--allowedTools", tool])

    log_kwargs: dict = dict(
        proposal_name=proposal_name, build_id=build.id,
        proposal_id=build.proposal_id,
        packet_sha256=packet_sha256, sandbox_path=sandbox,
        cmd=cmd, allowed_tools=ALLOWED_TOOLS,
    )

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            input=packet_content,
            capture_output=True,
            text=True,
            cwd=str(sandbox),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log_content = _build_log_content(
            **log_kwargs, timeout=True, timeout_secs=timeout,
        )
        write_artifact(
            conn, content=log_content,
            path=output_dir / f"build_log_{build.id}.md",
            kind=ArtifactKind.BUILD_LOG, related_id=build.proposal_id,
        )
        mark_build_failed(conn, build.id, summary=f"Timeout after {timeout}s")
        return False, sandbox

    # (c) Diff allowlist gate against sandbox baseline
    gate = diff_allowlist_gate(proposal_name, sandbox)

    # Record build log with all audit fields
    log_content = _build_log_content(
        **log_kwargs,
        exit_code=result.returncode,
        stdout=result.stdout, stderr=result.stderr,
        gate=gate,
    )
    write_artifact(
        conn, content=log_content,
        path=output_dir / f"build_log_{build.id}.md",
        kind=ArtifactKind.BUILD_LOG, related_id=build.proposal_id,
    )

    if not gate.ok:
        detail = (
            f"Allowed: {gate.allowed}, "
            f"Violations: {gate.violations}, "
            f"Missing: {gate.required_missing}"
        )
        mark_build_failed(conn, build.id, summary=f"Diff gate failed: {detail}")
        return False, sandbox

    # (d) Safe copy-back — rejects symlinks, traversal, unnormalized paths
    try:
        copied = _safe_copy_back(sandbox, project_root, gate.allowed)
    except ValueError as e:
        mark_build_failed(conn, build.id, summary=f"Copy-back rejected: {e}")
        return False, sandbox

    mark_build_succeeded(
        conn, build.id,
        summary=f"Build succeeded, copied: {', '.join(copied)}",
    )
    return True, sandbox


# ---------------------------------------------------------------------------
# Build status helpers
# ---------------------------------------------------------------------------


def mark_build_succeeded(
    conn: sqlite3.Connection,
    build_id: str,
    summary: str = "Build completed",
) -> None:
    """Mark a build as succeeded and update proposal status to BUILT."""
    build = Build.model_validate(
        dict(conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone())
    )
    update_build(
        conn, build_id,
        status=BuildStatus.SUCCEEDED,
        summary=summary,
    )
    update_proposal_status(conn, build.proposal_id, ProposalStatus.BUILT)


def mark_build_failed(
    conn: sqlite3.Connection,
    build_id: str,
    summary: str = "Build failed",
) -> None:
    """Mark a build as failed."""
    update_build(
        conn, build_id,
        status=BuildStatus.FAILED,
        summary=summary,
    )
