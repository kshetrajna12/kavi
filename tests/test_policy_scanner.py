"""Tests for policy scanner."""

from pathlib import Path

import pytest

from kavi.policies.scanner import Policy, scan_directory, scan_file


@pytest.fixture()
def policy():
    return Policy(
        forbidden_imports=["subprocess", "os.system", "pty", "paramiko"],
        allowed_network=False,
        allowed_write_paths=["./vault_out/", "./artifacts_out/"],
        forbid_dynamic_exec=True,
    )


@pytest.fixture()
def policy_from_yaml():
    policy_path = Path(__file__).parent.parent / "src" / "kavi" / "policies" / "policy.yaml"
    return Policy.from_yaml(policy_path)


def _write_py(tmp_path: Path, name: str, code: str) -> Path:
    p = tmp_path / name
    p.write_text(code)
    return p


class TestPolicyLoading:
    def test_from_yaml(self, policy_from_yaml):
        assert "subprocess" in policy_from_yaml.forbidden_imports
        assert policy_from_yaml.forbid_dynamic_exec is True
        assert policy_from_yaml.allowed_network is False


class TestForbiddenImports:
    def test_catches_subprocess(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "import subprocess\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1
        assert violations[0].rule == "forbidden_import"
        assert "subprocess" in violations[0].detail

    def test_catches_from_import(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "from subprocess import run\n")
        violations = scan_file(f, policy)
        assert len(violations) >= 1

    def test_catches_os_system(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "import os.system\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1

    def test_catches_paramiko(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "import paramiko\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1

    def test_allows_clean_imports(self, tmp_path, policy):
        f = _write_py(tmp_path, "good.py", "import json\nimport pathlib\n")
        violations = scan_file(f, policy)
        assert len(violations) == 0


class TestDynamicExec:
    def test_catches_eval(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "x = eval('1+1')\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1
        assert violations[0].rule == "forbid_dynamic_exec"

    def test_catches_exec(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "exec('print(1)')\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1

    def test_catches_compile(self, tmp_path, policy):
        f = _write_py(tmp_path, "bad.py", "compile('pass', '<string>', 'exec')\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1

    def test_allows_safe_code(self, tmp_path, policy):
        f = _write_py(tmp_path, "good.py", "x = 1 + 1\nprint(x)\n")
        violations = scan_file(f, policy)
        assert len(violations) == 0


class TestDirectoryScan:
    def test_scans_all_files(self, tmp_path, policy):
        _write_py(tmp_path, "a.py", "import json\n")
        _write_py(tmp_path, "b.py", "import pathlib\n")
        result = scan_directory(tmp_path, policy)
        assert result.files_scanned == 2
        assert result.ok is True

    def test_finds_violations_across_files(self, tmp_path, policy):
        _write_py(tmp_path, "good.py", "import json\n")
        _write_py(tmp_path, "bad.py", "import subprocess\neval('x')\n")
        result = scan_directory(tmp_path, policy)
        assert result.files_scanned == 2
        assert result.ok is False
        assert len(result.violations) == 2

    def test_scans_subdirectories(self, tmp_path, policy):
        sub = tmp_path / "sub"
        sub.mkdir()
        _write_py(sub, "deep.py", "import pty\n")
        result = scan_directory(tmp_path, policy)
        assert result.ok is False


class TestSyntaxErrors:
    def test_reports_syntax_error(self, tmp_path, policy):
        f = _write_py(tmp_path, "broken.py", "def foo(\n")
        violations = scan_file(f, policy)
        assert len(violations) == 1
        assert violations[0].rule == "syntax_error"
