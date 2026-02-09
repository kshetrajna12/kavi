"""Tests for artifact writer."""

from pathlib import Path

import pytest

from kavi.artifacts.writer import content_hash, write_artifact, write_note, write_skill_spec
from kavi.ledger.db import init_db
from kavi.ledger.models import ArtifactKind, get_artifacts_for_related


@pytest.fixture()
def db(tmp_path: Path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_content_different_hash(self):
        assert content_hash("hello") != content_hash("world")

    def test_returns_hex_string(self):
        h = content_hash("test")
        assert len(h) == 64  # sha256 hex digest


class TestWriteArtifact:
    def test_writes_file_and_records(self, db, tmp_path):
        path = tmp_path / "output" / "test.md"
        art = write_artifact(
            db, content="# Hello\n", path=path,
            kind=ArtifactKind.NOTE, related_id="test-123",
        )
        assert path.exists()
        assert path.read_text() == "# Hello\n"
        assert art.sha256 == content_hash("# Hello\n")
        assert art.related_id == "test-123"

    def test_creates_parent_directories(self, db, tmp_path):
        path = tmp_path / "deep" / "nested" / "dir" / "file.md"
        write_artifact(db, content="x", path=path, kind=ArtifactKind.NOTE)
        assert path.exists()

    def test_recorded_in_ledger(self, db, tmp_path):
        path = tmp_path / "test.md"
        art = write_artifact(
            db, content="content", path=path,
            kind=ArtifactKind.SKILL_SPEC, related_id="prop-1",
        )
        artifacts = get_artifacts_for_related(db, "prop-1")
        assert len(artifacts) == 1
        assert artifacts[0].id == art.id


class TestWriteSkillSpec:
    def test_writes_spec_markdown(self, db, tmp_path):
        art = write_skill_spec(
            db,
            name="write_note",
            description="Write a markdown note",
            io_schema='{"input": {}, "output": {}}',
            side_effect_class="FILE_WRITE",
            required_secrets="[]",
            proposal_id="prop-abc",
            output_dir=tmp_path,
        )
        spec_path = Path(art.path)
        assert spec_path.exists()
        content = spec_path.read_text()
        assert "write_note" in content
        assert "FILE_WRITE" in content
        assert art.kind == ArtifactKind.SKILL_SPEC


class TestWriteNote:
    def test_writes_obsidian_compatible_note(self, db, tmp_path):
        path = tmp_path / "Inbox" / "AI" / "test.md"
        art = write_note(
            db, title="Test Note", body="Hello world", path=path,
        )
        assert path.exists()
        content = path.read_text()
        assert "title: Test Note" in content
        assert "Hello world" in content
        assert art.kind == ArtifactKind.NOTE
