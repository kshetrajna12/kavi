"""Tests for the invariant checker."""

from pathlib import Path

from kavi.forge.invariants import (
    _check_extended_safety,
    _check_structural,
    check_invariants,
)

# --- Structural conformance tests ---

VALID_SKILL = '''\
from pydantic import BaseModel
from kavi.skills.base import BaseSkill

class NoteInput(BaseModel):
    path: str

class NoteOutput(BaseModel):
    written_path: str

class WriteNoteSkill(BaseSkill):
    name = "write_note"
    description = "Write a note"
    input_model = NoteInput
    output_model = NoteOutput
    side_effect_class = "FILE_WRITE"

    def execute(self, input_data):
        pass
'''

MISSING_ATTRS_SKILL = '''\
from kavi.skills.base import BaseSkill

class BadSkill(BaseSkill):
    name = "bad"
    description = "Bad skill"

    def execute(self, input_data):
        pass
'''

NO_BASE_SKILL = '''\
class NotASkill:
    name = "nope"
'''

WRONG_SIDE_EFFECT = '''\
from kavi.skills.base import BaseSkill
from pydantic import BaseModel

class X(BaseModel):
    pass

class MySkill(BaseSkill):
    name = "my_skill"
    description = "desc"
    input_model = X
    output_model = X
    side_effect_class = "NETWORK"

    def execute(self, input_data):
        pass
'''


class TestStructuralConformance:
    def test_valid_skill(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(VALID_SKILL)
        violations = _check_structural(f, "FILE_WRITE")
        assert violations == []

    def test_missing_attrs(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(MISSING_ATTRS_SKILL)
        violations = _check_structural(f, "FILE_WRITE")
        assert len(violations) == 1
        assert "Missing required attrs" in violations[0].message
        assert "input_model" in violations[0].message

    def test_no_baseskill_class(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(NO_BASE_SKILL)
        violations = _check_structural(f, "")
        assert len(violations) == 1
        assert "No class extending BaseSkill" in violations[0].message

    def test_wrong_side_effect(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(WRONG_SIDE_EFFECT)
        violations = _check_structural(f, "FILE_WRITE")
        assert len(violations) == 1
        assert "NETWORK" in violations[0].message
        assert "FILE_WRITE" in violations[0].message

    def test_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "missing.py"
        violations = _check_structural(f, "FILE_WRITE")
        assert len(violations) == 1
        assert "not found" in violations[0].message

    def test_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text("def broken(:\n")
        violations = _check_structural(f, "")
        assert len(violations) == 1
        assert "Syntax error" in violations[0].message


# --- Extended safety tests ---

IMPORT_DUNDER = '''\
m = __import__("os")
'''

IMPORTLIB_CALL = '''\
import importlib
m = importlib.import_module("os")
'''

CLEAN_CODE = '''\
import os
x = 1
'''


class TestExtendedSafety:
    def test_dunder_import(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(IMPORT_DUNDER)
        violations = _check_extended_safety(f)
        assert len(violations) == 1
        assert "__import__" in violations[0].message

    def test_importlib(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(IMPORTLIB_CALL)
        violations = _check_extended_safety(f)
        assert len(violations) == 1
        assert "importlib.import_module" in violations[0].message

    def test_clean_code(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(CLEAN_CODE)
        violations = _check_extended_safety(f)
        assert violations == []


# --- Top-level orchestrator ---


class TestCheckInvariants:
    def test_all_pass(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(VALID_SKILL)
        result = check_invariants(
            f,
            expected_side_effect="FILE_WRITE",
            proposal_name="write_note",
            project_root=tmp_path,
        )
        assert result.ok is True
        assert result.structural_ok is True
        assert result.scope_ok is True
        assert result.safety_ok is True

    def test_structural_fail(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(NO_BASE_SKILL)
        result = check_invariants(
            f,
            expected_side_effect="",
            proposal_name="test",
            project_root=tmp_path,
        )
        assert result.ok is False
        assert result.structural_ok is False

    def test_safety_fail(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(IMPORT_DUNDER)
        result = check_invariants(
            f,
            expected_side_effect="",
            proposal_name="test",
            project_root=tmp_path,
        )
        assert result.ok is False
        assert result.safety_ok is False

    def test_combined_violations(self, tmp_path: Path) -> None:
        f = tmp_path / "skill.py"
        f.write_text(NO_BASE_SKILL + "\n" + IMPORT_DUNDER)
        result = check_invariants(
            f,
            expected_side_effect="",
            proposal_name="test",
            project_root=tmp_path,
        )
        assert result.ok is False
        assert result.structural_ok is False
        assert result.safety_ok is False
        assert len(result.violations) >= 2
