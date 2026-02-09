"""Tests for sandboxed Claude Code build invocation (D009)."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kavi.forge.build import (
    _safe_copy_back,
    build_skill,
    create_sandbox,
    invoke_claude_build,
)
from kavi.forge.propose import propose_skill
from kavi.ledger.db import init_db
from kavi.ledger.models import (
    BuildStatus,
    ProposalStatus,
    SideEffectClass,
    get_build,
    get_proposal,
)

IO_SCHEMA = '{"input": {"path": "str"}, "output": {"written_path": "str"}}'


@pytest.fixture()
def db(tmp_path: Path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture()
def artifacts_dir(tmp_path: Path):
    d = tmp_path / "artifacts_out"
    d.mkdir()
    return d


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo for sandbox tests."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path), capture_output=True, check=True,
    )


# ---------------------------------------------------------------------------
# Sandbox creation tests
# ---------------------------------------------------------------------------


class TestCreateSandbox:
    """Tests for sandbox workspace creation."""

    def test_creates_repo_copy(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "hello.py").write_text("print('hi')")
        _init_git_repo(project)

        sandbox_parent = tmp_path / "sandbox"
        sandbox_parent.mkdir()
        sandbox = create_sandbox(project, sandbox_parent)

        assert (sandbox / "src" / "hello.py").exists()
        assert (sandbox / ".git").is_dir()

    def test_excludes_dot_git(self, tmp_path: Path) -> None:
        """Original .git is NOT copied — sandbox has a fresh git repo."""
        project = tmp_path / "project"
        project.mkdir()
        _init_git_repo(project)
        # Add a remote and hook to the original — neither should carry over
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(project), capture_output=True, check=True,
        )
        hooks_dir = project / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho pwned")

        sandbox_parent = tmp_path / "sandbox"
        sandbox_parent.mkdir()
        sandbox = create_sandbox(project, sandbox_parent)

        # Fresh git repo with zero remotes, zero hooks
        result = subprocess.run(
            ["git", "remote"], cwd=str(sandbox),
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""
        # Fresh git has no hooks directory (or it's empty default)
        hooks = sandbox / ".git" / "hooks"
        if hooks.exists():
            assert not any(f for f in hooks.iterdir() if not f.name.endswith(".sample"))

    def test_has_baseline_commit(self, tmp_path: Path) -> None:
        """Sandbox has exactly one commit (the baseline)."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "hello.py").write_text("print('hi')")
        _init_git_repo(project)

        sandbox_parent = tmp_path / "sandbox"
        sandbox_parent.mkdir()
        sandbox = create_sandbox(project, sandbox_parent)

        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(sandbox), capture_output=True, text=True, check=True,
        )
        lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "sandbox baseline" in lines[0]

    def test_strips_secret_files(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text("SECRET=hunter2")
        (project / "key.pem").write_text("-----BEGIN RSA-----")
        (project / "credentials.json").write_text("{}")
        (project / "safe.py").write_text("# ok")
        _init_git_repo(project)

        sandbox_parent = tmp_path / "sandbox"
        sandbox_parent.mkdir()
        sandbox = create_sandbox(project, sandbox_parent)

        assert not (sandbox / ".env").exists()
        assert not (sandbox / "key.pem").exists()
        assert not (sandbox / "credentials.json").exists()
        assert (sandbox / "safe.py").exists()

    def test_clean_working_tree(self, tmp_path: Path) -> None:
        """After baseline commit, working tree is clean."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "hello.py").write_text("print('hi')")
        _init_git_repo(project)

        sandbox_parent = tmp_path / "sandbox"
        sandbox_parent.mkdir()
        sandbox = create_sandbox(project, sandbox_parent)

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(sandbox), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Copy-back safety tests
# ---------------------------------------------------------------------------


class TestSafeCopyBack:
    """Tests for _safe_copy_back symlink and traversal checks."""

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        target = tmp_path / "secret.txt"
        target.write_text("sensitive data")
        (sandbox / "src" / "kavi" / "skills").mkdir(parents=True)
        os.symlink(str(target), str(sandbox / "src" / "kavi" / "skills" / "evil.py"))

        project = tmp_path / "project"
        project.mkdir()

        with pytest.raises(ValueError, match="symlink"):
            _safe_copy_back(
                sandbox, project,
                ["src/kavi/skills/evil.py"],
            )

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        (sandbox / "src" / "kavi" / "skills").mkdir(parents=True)
        (sandbox / "src" / "kavi" / "skills" / "ok.py").write_text("# fine")

        project = tmp_path / "project"
        project.mkdir()

        with pytest.raises(ValueError, match="traversal"):
            _safe_copy_back(
                sandbox, project,
                ["../../../etc/shadow"],
            )

    def test_copies_normal_files(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        (sandbox / "src" / "kavi" / "skills").mkdir(parents=True)
        (sandbox / "src" / "kavi" / "skills" / "write_note.py").write_text("# skill")

        project = tmp_path / "project"
        project.mkdir()

        copied = _safe_copy_back(
            sandbox, project,
            ["src/kavi/skills/write_note.py"],
        )
        assert len(copied) == 1
        assert "src/kavi/skills/write_note.py" in copied[0]
        assert "create" in copied[0]
        assert (project / "src" / "kavi" / "skills" / "write_note.py").exists()


# ---------------------------------------------------------------------------
# Claude Code invocation tests (mocked)
# ---------------------------------------------------------------------------


def _make_claude_mock(side_effect_fn=None):
    """Create a mock that intercepts only `claude` calls, passes git through.

    If side_effect_fn is provided, it's called with (cmd, **kwargs) for claude calls.
    """
    _real_run = subprocess.run

    def smart_run(cmd, **kwargs):
        # Pass git commands through to real subprocess
        if isinstance(cmd, list) and cmd and cmd[0] in ("git",):
            return _real_run(cmd, **kwargs)
        # Intercept claude calls
        if side_effect_fn is not None:
            return side_effect_fn(cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return smart_run


class TestInvokeClaudeBuild:
    """Tests for invoke_claude_build (mocked subprocess)."""

    def _setup(self, db, artifacts_dir, tmp_path):
        """Create a proposal, build record, and git-initialized project root."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "src" / "kavi" / "skills").mkdir(parents=True)
        (project / "tests").mkdir()
        _init_git_repo(project)

        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, artifact = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        return proposal, build, artifact, project

    def test_claude_not_on_path(self, db, artifacts_dir, tmp_path):
        proposal, build, artifact, project = self._setup(db, artifacts_dir, tmp_path)

        with patch("kavi.forge.build.shutil.which", return_value=None):
            success, sandbox = invoke_claude_build(
                db, build=build, proposal_name=proposal.name,
                build_packet_path=Path(artifact.path),
                project_root=project, output_dir=artifacts_dir,
            )

        assert success is False
        assert sandbox is None
        updated = get_build(db, build.id)
        assert updated is not None
        assert updated.status == BuildStatus.FAILED

    def test_successful_build(self, db, artifacts_dir, tmp_path):
        """Claude generates only the allowed files in the sandbox."""
        proposal, build, artifact, project = self._setup(db, artifacts_dir, tmp_path)

        def claude_writes_files(cmd, **kwargs):
            cwd = Path(kwargs.get("cwd", "."))
            skill_dir = cwd / "src" / "kavi" / "skills"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "write_note.py").write_text("# skill code")
            test_dir = cwd / "tests"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / "test_skill_write_note.py").write_text("# test code")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Done", stderr="")

        with (
            patch("kavi.forge.build.shutil.which", return_value="/usr/bin/claude"),
            patch("kavi.forge.build.subprocess.run",
                  side_effect=_make_claude_mock(claude_writes_files)),
        ):
            success, sandbox = invoke_claude_build(
                db, build=build, proposal_name=proposal.name,
                build_packet_path=Path(artifact.path),
                project_root=project, output_dir=artifacts_dir,
            )

        assert success is True
        assert (project / "src" / "kavi" / "skills" / "write_note.py").exists()
        assert (project / "tests" / "test_skill_write_note.py").exists()
        updated_proposal = get_proposal(db, proposal.id)
        assert updated_proposal is not None
        assert updated_proposal.status == ProposalStatus.BUILT

    def test_build_with_disallowed_changes_fails(self, db, artifacts_dir, tmp_path):
        """Claude modifies files outside allowlist -> gate fails."""
        proposal, build, artifact, project = self._setup(db, artifacts_dir, tmp_path)

        def claude_tampers(cmd, **kwargs):
            cwd = Path(kwargs.get("cwd", "."))
            skill_dir = cwd / "src" / "kavi" / "skills"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "write_note.py").write_text("# skill")
            test_dir = cwd / "tests"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / "test_skill_write_note.py").write_text("# test")
            (cwd / "pyproject.toml").write_text("# hacked")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Done", stderr="")

        with (
            patch("kavi.forge.build.shutil.which", return_value="/usr/bin/claude"),
            patch("kavi.forge.build.subprocess.run",
                  side_effect=_make_claude_mock(claude_tampers)),
        ):
            success, sandbox = invoke_claude_build(
                db, build=build, proposal_name=proposal.name,
                build_packet_path=Path(artifact.path),
                project_root=project, output_dir=artifacts_dir,
            )

        assert success is False
        assert not (project / "src" / "kavi" / "skills" / "write_note.py").exists()
        updated = get_build(db, build.id)
        assert updated is not None
        assert updated.status == BuildStatus.FAILED
        assert "Diff gate" in (updated.summary or "")

    def test_build_timeout(self, db, artifacts_dir, tmp_path):
        proposal, build, artifact, project = self._setup(db, artifacts_dir, tmp_path)

        def claude_hangs(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 600))

        with (
            patch("kavi.forge.build.shutil.which", return_value="/usr/bin/claude"),
            patch("kavi.forge.build.subprocess.run",
                  side_effect=_make_claude_mock(claude_hangs)),
        ):
            success, sandbox = invoke_claude_build(
                db, build=build, proposal_name=proposal.name,
                build_packet_path=Path(artifact.path),
                project_root=project, output_dir=artifacts_dir,
            )

        assert success is False
        updated = get_build(db, build.id)
        assert updated is not None
        assert updated.status == BuildStatus.FAILED
        assert "Timeout" in (updated.summary or "")

    def test_build_log_includes_audit_fields(self, db, artifacts_dir, tmp_path):
        """Build log contains packet hash, sandbox path, command, and gate verdict."""
        proposal, build, artifact, project = self._setup(db, artifacts_dir, tmp_path)

        def claude_writes_files(cmd, **kwargs):
            cwd = Path(kwargs.get("cwd", "."))
            skill_dir = cwd / "src" / "kavi" / "skills"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "write_note.py").write_text("# skill")
            test_dir = cwd / "tests"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / "test_skill_write_note.py").write_text("# test")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Generated", stderr="")

        with (
            patch("kavi.forge.build.shutil.which", return_value="/usr/bin/claude"),
            patch("kavi.forge.build.subprocess.run",
                  side_effect=_make_claude_mock(claude_writes_files)),
        ):
            invoke_claude_build(
                db, build=build, proposal_name=proposal.name,
                build_packet_path=Path(artifact.path),
                project_root=project, output_dir=artifacts_dir,
            )

        log_path = artifacts_dir / f"build_log_{build.id}.md"
        assert log_path.exists()
        content = log_path.read_text()
        # Audit fields
        assert "Packet SHA256" in content
        assert "Sandbox" in content
        assert "Command" in content
        assert "Diff Allowlist Gate: PASS" in content
        assert "Generated" in content
