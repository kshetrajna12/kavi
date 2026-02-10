"""Tests for kavi doctor healthchecks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from kavi.ops.doctor import (
    CheckResult,
    DoctorReport,
    check_config_paths,
    check_execution_log_path,
    check_log_sanity,
    check_registry_integrity,
    check_registry_path,
    check_sparkstation,
    check_vault_path,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# CheckResult / DoctorReport basics
# ---------------------------------------------------------------------------


class TestDoctorReport:
    def test_overall_ok_when_all_ok(self) -> None:
        report = DoctorReport(checks=[
            CheckResult("a", "ok", "good"),
            CheckResult("b", "ok", "fine"),
        ])
        assert report.overall_status == "ok"

    def test_overall_warn_when_any_warn(self) -> None:
        report = DoctorReport(checks=[
            CheckResult("a", "ok", "good"),
            CheckResult("b", "warn", "meh"),
        ])
        assert report.overall_status == "warn"

    def test_overall_fail_when_any_fail(self) -> None:
        report = DoctorReport(checks=[
            CheckResult("a", "ok", "good"),
            CheckResult("b", "warn", "meh"),
            CheckResult("c", "fail", "bad"),
        ])
        assert report.overall_status == "fail"

    def test_to_dict_structure(self) -> None:
        report = DoctorReport(checks=[
            CheckResult("a", "ok", "good", None),
            CheckResult("b", "fail", "bad", "fix it"),
        ])
        d = report.to_dict()
        assert d["overall_status"] == "fail"
        assert len(d["checks"]) == 2
        assert d["checks"][1]["remediation"] == "fix it"
        assert "timestamp" in d


# ---------------------------------------------------------------------------
# Check 1: Config + paths
# ---------------------------------------------------------------------------


class TestConfigPaths:
    def test_vault_exists(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault_out"
        vault.mkdir()
        result = check_vault_path(vault)
        assert result.status == "ok"

    def test_vault_missing(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault_out"
        result = check_vault_path(vault)
        assert result.status == "fail"
        assert "mkdir" in (result.remediation or "")

    def test_registry_exists(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.yaml"
        reg.write_text("skills: []")
        result = check_registry_path(reg)
        assert result.status == "ok"

    def test_registry_missing(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.yaml"
        result = check_registry_path(reg)
        assert result.status == "fail"

    def test_log_writable(self, tmp_path: Path) -> None:
        log = tmp_path / "executions.jsonl"
        log.write_text("")
        result = check_execution_log_path(log)
        assert result.status == "ok"

    def test_log_parent_writable(self, tmp_path: Path) -> None:
        log = tmp_path / "subdir" / "executions.jsonl"
        (tmp_path / "subdir").mkdir()
        result = check_execution_log_path(log)
        assert result.status == "ok"

    def test_log_parent_missing(self, tmp_path: Path) -> None:
        log = Path("/nonexistent/path/executions.jsonl")
        result = check_execution_log_path(log)
        assert result.status == "warn"

    def test_check_config_paths_returns_all(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault_out"
        vault.mkdir()
        reg = tmp_path / "registry.yaml"
        reg.write_text("skills: []")
        log = tmp_path / "executions.jsonl"
        results = check_config_paths(vault, reg, log)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Check 2: Registry integrity
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    def test_valid_registry_all_hashes_match(self, tmp_path: Path) -> None:
        # Create a fake skill module
        skill_dir = tmp_path / "fakeskill"
        skill_dir.mkdir()
        skill_file = skill_dir / "myplugin.py"
        skill_file.write_text("# skill code\n")
        init_file = skill_dir / "__init__.py"
        init_file.write_text("")

        import hashlib

        expected = hashlib.sha256(skill_file.read_bytes()).hexdigest()

        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "skills:\n"
            "- name: test_skill\n"
            "  module_path: fakeskill.myplugin.TestSkill\n"
            f"  hash: {expected}\n"
        )

        # Mock importlib to return our fake module
        fake_mod = MagicMock()
        fake_mod.__file__ = str(skill_file)

        with patch("kavi.ops.doctor.importlib.import_module", return_value=fake_mod):
            results = check_registry_integrity(reg)

        # Should have parse ok + skill ok
        statuses = {r.name: r.status for r in results}
        assert statuses["registry_parse"] == "ok"
        assert statuses["skill_test_skill"] == "ok"

    def test_hash_drift_detected(self, tmp_path: Path) -> None:
        skill_file = tmp_path / "drifted.py"
        skill_file.write_text("# changed code\n")

        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "skills:\n"
            "- name: drifted_skill\n"
            "  module_path: fakeskill.drifted.DriftedSkill\n"
            "  hash: deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        )

        fake_mod = MagicMock()
        fake_mod.__file__ = str(skill_file)

        with patch("kavi.ops.doctor.importlib.import_module", return_value=fake_mod):
            results = check_registry_integrity(reg)

        drift = [r for r in results if r.name == "skill_drifted_skill"]
        assert len(drift) == 1
        assert drift[0].status == "fail"
        assert "hash drift" in drift[0].message
        rem = (drift[0].remediation or "")
        assert "Re-verify" in rem or "re-promote" in rem.lower()

    def test_missing_registry_file(self, tmp_path: Path) -> None:
        reg = tmp_path / "nonexistent.yaml"
        results = check_registry_integrity(reg)
        assert len(results) == 1
        assert results[0].status == "fail"
        assert results[0].name == "registry_parse"

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        reg = tmp_path / "bad.yaml"
        reg.write_text(": : : invalid yaml {{{}}")
        results = check_registry_integrity(reg)
        # yaml.safe_load may parse this oddly or fail
        assert len(results) >= 1

    def test_duplicate_names_flagged(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "skills:\n"
            "- name: foo\n"
            "  module_path: a.b.C\n"
            "- name: foo\n"
            "  module_path: a.b.D\n"
        )

        fake_mod = MagicMock()
        fake_mod.__file__ = None

        with patch("kavi.ops.doctor.importlib.import_module", return_value=fake_mod):
            results = check_registry_integrity(reg)

        dupe_results = [r for r in results if r.name == "registry_duplicates"]
        assert len(dupe_results) == 1
        assert dupe_results[0].status == "fail"
        assert "foo" in dupe_results[0].message

    def test_no_hash_warns(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "skills:\n"
            "- name: nohash\n"
            "  module_path: some.module.Cls\n"
        )

        fake_mod = MagicMock()
        fake_mod.__file__ = "/some/path.py"

        with patch("kavi.ops.doctor.importlib.import_module", return_value=fake_mod):
            results = check_registry_integrity(reg)

        nohash = [r for r in results if r.name == "skill_nohash"]
        assert len(nohash) == 1
        assert nohash[0].status == "warn"
        assert "no hash" in nohash[0].message

    def test_import_failure(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "skills:\n"
            "- name: broken\n"
            "  module_path: nonexistent.module.Cls\n"
            "  hash: abc123\n"
        )

        with patch(
            "kavi.ops.doctor.importlib.import_module",
            side_effect=ModuleNotFoundError("No module named 'nonexistent'"),
        ):
            results = check_registry_integrity(reg)

        broken = [r for r in results if r.name == "skill_broken"]
        assert len(broken) == 1
        assert broken[0].status == "fail"
        assert "import failed" in broken[0].message


# ---------------------------------------------------------------------------
# Check 3: Sparkstation
# ---------------------------------------------------------------------------


class TestSparkstation:
    @patch("kavi.ops.doctor.OpenAI")
    def test_spark_reachable(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        result = check_sparkstation("http://localhost:8000/v1", timeout=0.5)
        assert result.status == "ok"

    @patch("kavi.ops.doctor.OpenAI")
    def test_spark_unreachable(self, mock_cls: MagicMock) -> None:
        mock_cls.side_effect = Exception("Connection refused")
        result = check_sparkstation("http://localhost:8000/v1", timeout=0.5)
        assert result.status == "warn"
        assert "unreachable" in result.message.lower()
        assert "fallback" in result.message.lower()

    def test_spark_unreachable_no_mock(self) -> None:
        # Use a port that's almost certainly not listening
        result = check_sparkstation("http://127.0.0.1:19999/v1", timeout=0.3)
        assert result.status == "warn"


# ---------------------------------------------------------------------------
# Check 4: Toolchain (just test Python version since it's deterministic)
# ---------------------------------------------------------------------------


class TestToolchain:
    def test_python_version_check(self) -> None:
        from kavi.ops.doctor import check_toolchain

        results = check_toolchain()
        py = [r for r in results if r.name == "python_version"]
        assert len(py) == 1
        # We're running on 3.12+, so should be ok
        assert py[0].status == "ok"


# ---------------------------------------------------------------------------
# Check 5: Log sanity
# ---------------------------------------------------------------------------


class TestLogSanity:
    def test_no_log_file(self, tmp_path: Path) -> None:
        log = tmp_path / "executions.jsonl"
        result = check_log_sanity(log)
        assert result.status == "ok"
        assert "No execution log yet" in result.message

    def test_all_valid_records(self, tmp_path: Path) -> None:
        log = tmp_path / "executions.jsonl"
        records = [
            json.dumps({"v": 1, "skill_name": "test"}),
            json.dumps({"v": 1, "skill_name": "test2"}),
        ]
        log.write_text("\n".join(records) + "\n")
        result = check_log_sanity(log)
        assert result.status == "ok"
        assert "2 valid" in result.message

    def test_malformed_lines_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "executions.jsonl"
        lines = [
            json.dumps({"v": 1, "skill_name": "ok"}),
            "this is not json",
            json.dumps({"v": 1, "skill_name": "also_ok"}),
            "{broken json",
        ]
        log.write_text("\n".join(lines) + "\n")
        result = check_log_sanity(log)
        assert result.status == "warn"
        assert "2 malformed" in result.message
        assert "2 valid" in result.message

    def test_record_version_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "executions.jsonl"
        lines = [
            json.dumps({"v": 1, "skill_name": "a"}),
            json.dumps({"skill_name": "b"}),  # no version field
        ]
        log.write_text("\n".join(lines) + "\n")
        result = check_log_sanity(log)
        assert result.status == "ok"
        assert "1 with record_version" in result.message


# ---------------------------------------------------------------------------
# Integration: run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    def test_happy_path(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault_out"
        vault.mkdir()
        reg = tmp_path / "registry.yaml"
        reg.write_text("skills: []\n")
        log = tmp_path / "executions.jsonl"

        report = run_all_checks(
            vault_out=vault,
            registry_path=reg,
            log_path=log,
            spark_base_url="http://127.0.0.1:19999/v1",
            spark_timeout=0.3,
        )

        assert report.overall_status in ("ok", "warn")
        # Spark will be warn (unreachable), everything else ok
        names = {c.name for c in report.checks}
        assert "vault_path" in names
        assert "registry_parse" in names
        assert "sparkstation" in names
        assert "log_sanity" in names

    def test_missing_vault_causes_fail(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault_out"  # not created
        reg = tmp_path / "registry.yaml"
        reg.write_text("skills: []\n")
        log = tmp_path / "executions.jsonl"

        report = run_all_checks(
            vault_out=vault,
            registry_path=reg,
            log_path=log,
            spark_base_url="http://127.0.0.1:19999/v1",
            spark_timeout=0.3,
        )

        assert report.overall_status == "fail"
        vault_check = [c for c in report.checks if c.name == "vault_path"][0]
        assert vault_check.status == "fail"
        assert "mkdir" in (vault_check.remediation or "")
