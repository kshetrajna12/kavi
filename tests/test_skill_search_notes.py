"""Tests for the search_notes skill."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kavi.llm.spark import SparkUnavailableError
from kavi.skills import search_notes
from kavi.skills.search_notes import (
    SearchNotesInput,
    SearchNotesOutput,
    SearchNotesSkill,
    SearchResult,
    _cosine_similarity,
    _has_tag,
    _lexical_score,
    _snippet,
    extract_title,
)


@pytest.fixture(autouse=True)
def _isolate_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_notes, "VAULT_OUT", tmp_path / "vault_out")


def _vault(tmp_path: Path) -> Path:
    return tmp_path / "vault_out"


def _write_note(tmp_path: Path, rel: str, content: str) -> Path:
    vault = _vault(tmp_path)
    dest = vault / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestSearchNotesModels:
    def test_input_defaults(self) -> None:
        inp = SearchNotesInput(query="test")
        assert inp.top_k == 5
        assert inp.max_chars == 12000
        assert inp.timeout_s == 8.0
        assert inp.include_snippet is True
        assert inp.tag is None

    def test_input_custom(self) -> None:
        inp = SearchNotesInput(
            query="hello",
            top_k=10,
            max_chars=5000,
            timeout_s=3.0,
            include_snippet=False,
            tag="work",
        )
        assert inp.top_k == 10
        assert inp.max_chars == 5000
        assert inp.timeout_s == 3.0
        assert inp.include_snippet is False
        assert inp.tag == "work"

    def test_top_k_clamped_low(self) -> None:
        with pytest.raises(Exception):
            SearchNotesInput(query="q", top_k=0)

    def test_top_k_clamped_high(self) -> None:
        with pytest.raises(Exception):
            SearchNotesInput(query="q", top_k=21)

    def test_output_model(self) -> None:
        out = SearchNotesOutput(
            query="test",
            results=[],
            truncated_paths=[],
            used_model="bge-large",
        )
        assert out.error is None

    def test_output_with_error(self) -> None:
        out = SearchNotesOutput(
            query="test",
            results=[],
            truncated_paths=[],
            used_model="lexical-fallback",
            error="SPARKSTATION_UNAVAILABLE",
        )
        assert out.error == "SPARKSTATION_UNAVAILABLE"

    def test_search_result_model(self) -> None:
        r = SearchResult(path="note.md", score=0.95, title="My Note", snippet="some text")
        assert r.path == "note.md"
        assert r.score == 0.95

    def test_search_result_optional_fields(self) -> None:
        r = SearchResult(path="note.md", score=0.5)
        assert r.title is None
        assert r.snippet is None


# ---------------------------------------------------------------------------
# Skill attribute tests
# ---------------------------------------------------------------------------


class TestSearchNotesAttributes:
    def test_attributes(self) -> None:
        skill = SearchNotesSkill()
        assert skill.name == "search_notes"
        assert skill.side_effect_class == "READ_ONLY"
        assert skill.input_model is SearchNotesInput
        assert skill.output_model is SearchNotesOutput


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_title_with_h1(self) -> None:
        assert extract_title("# My Title\n\nBody") == "My Title"

    def test_extract_title_meeting_notes(self) -> None:
        note = "# Meeting Notes\n\nDiscussed roadmap.\n- Item 1\n- Item 2"
        assert extract_title(note) == "Meeting Notes"

    def test_extract_title_none(self) -> None:
        assert extract_title("No heading here") is None

    def test_extract_title_none_for_h2(self) -> None:
        assert extract_title("## Not an H1\n\nBody") is None

    def test_extract_title_empty_heading(self) -> None:
        assert extract_title("# \n\nBody text") is None

    def test_extract_title_never_contains_newline(self) -> None:
        # Regression: title must be single-line even if content is weird
        assert extract_title("# Clean Title\nBody") == "Clean Title"
        result = extract_title("# Title")
        assert result is not None
        assert "\n" not in result
        assert "\r" not in result

    def test_extract_title_leading_whitespace(self) -> None:
        assert extract_title("  # Indented Heading\n\nBody") == "Indented Heading"

    def test_has_tag_present(self) -> None:
        assert _has_tag("Some text #work here", "work") is True

    def test_has_tag_absent(self) -> None:
        assert _has_tag("Some text here", "work") is False

    def test_has_tag_not_substring(self) -> None:
        assert _has_tag("Some #working text", "work") is False

    def test_has_tag_heading_not_matched(self) -> None:
        assert _has_tag("# work\nBody", "work") is False

    def test_snippet_with_match(self) -> None:
        content = "A" * 100 + "hello world" + "B" * 100
        s = _snippet(content, "hello")
        assert "hello" in s

    def test_snippet_no_match(self) -> None:
        content = "Some content here"
        s = _snippet(content, "xyz")
        assert s == content

    def test_cosine_identical(self) -> None:
        v = [1.0, 0.0, 1.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_orthogonal(self) -> None:
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_cosine_zero_vector(self) -> None:
        assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_lexical_score_all_match(self) -> None:
        assert _lexical_score("hello world test", "hello world") == 1.0

    def test_lexical_score_partial(self) -> None:
        assert _lexical_score("hello planet", "hello world") == 0.5

    def test_lexical_score_no_match(self) -> None:
        assert _lexical_score("nothing here", "xyz abc") == 0.0

    def test_lexical_score_empty_query(self) -> None:
        assert _lexical_score("content", "") == 0.0


# ---------------------------------------------------------------------------
# Execute: empty / edge cases
# ---------------------------------------------------------------------------


class TestExecuteEdgeCases:
    def test_empty_query(self) -> None:
        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query=""))
        assert result.results == []
        assert result.error == "EMPTY_QUERY"
        assert result.used_model == "none"

    def test_whitespace_query(self) -> None:
        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="   "))
        assert result.results == []
        assert result.error == "EMPTY_QUERY"

    def test_vault_missing(self) -> None:
        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="anything"))
        assert result.results == []
        assert result.truncated_paths == []

    def test_empty_vault(self, tmp_path: Path) -> None:
        _vault(tmp_path).mkdir(parents=True)
        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="anything"))
        assert result.results == []


# ---------------------------------------------------------------------------
# Semantic search (mocked embeddings)
# ---------------------------------------------------------------------------


def _fake_embed(texts: list[str], **kwargs: object) -> list[list[float]]:
    """Return simple fake embeddings: each text gets a vector based on length."""
    return [[float(len(t)), 1.0, 0.0] for t in texts]


class TestSemanticSearch:
    @patch("kavi.skills.search_notes.embed")
    def test_basic_semantic_search(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "close.md", "# Close\n\nThis is about machine learning")
        _write_note(tmp_path, "far.md", "# Far\n\nCooking recipes for dinner")

        # Query embedding + 2 note embeddings
        # Make the first note closer to the query
        mock_embed.return_value = [
            [1.0, 0.0, 0.0],  # query
            [0.9, 0.1, 0.0],  # close.md — high similarity
            [0.0, 1.0, 0.0],  # far.md — low similarity
        ]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="machine learning"))

        assert len(result.results) == 2
        assert result.results[0].path == "close.md"
        assert result.results[0].score > result.results[1].score
        assert result.used_model == "bge-large"
        assert result.error is None

    @patch("kavi.skills.search_notes.embed")
    def test_top_k_limits_results(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        for i in range(5):
            _write_note(tmp_path, f"note{i}.md", f"# Note {i}\n\nContent {i}")

        mock_embed.return_value = [
            [1.0, 0.0],  # query
            [0.9, 0.0],
            [0.8, 0.0],
            [0.7, 0.0],
            [0.6, 0.0],
            [0.5, 0.0],
        ]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="test", top_k=2))

        assert len(result.results) == 2

    @patch("kavi.skills.search_notes.embed")
    def test_include_snippet_false(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# Note\n\nSome content here")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(
            SearchNotesInput(query="content", include_snippet=False)
        )

        assert len(result.results) == 1
        assert result.results[0].snippet is None

    @patch("kavi.skills.search_notes.embed")
    def test_truncated_notes_reported(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "big.md", "x" * 200)
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="test", max_chars=50))

        assert "big.md" in result.truncated_paths

    @patch("kavi.skills.search_notes.embed")
    def test_passes_timeout(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# Note\n\nContent")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        skill.execute(SearchNotesInput(query="test", timeout_s=3.5))

        _, kwargs = mock_embed.call_args
        assert kwargs["timeout"] == 3.5

    @patch("kavi.skills.search_notes.embed")
    def test_title_extracted(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# My Title\n\nBody text")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="body"))

        assert result.results[0].title == "My Title"

    @patch("kavi.skills.search_notes.embed")
    def test_no_title(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "No heading here just text")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="text"))

        assert result.results[0].title is None


# ---------------------------------------------------------------------------
# Fallback (lexical) search
# ---------------------------------------------------------------------------


class TestLexicalFallback:
    @patch("kavi.skills.search_notes.embed")
    def test_fallback_on_spark_unavailable(
        self, mock_embed: MagicMock, tmp_path: Path
    ) -> None:
        _write_note(tmp_path, "match.md", "# Match\n\nThis discusses python coding")
        _write_note(tmp_path, "no_match.md", "# Other\n\nCooking recipes")
        mock_embed.side_effect = SparkUnavailableError("down")

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="python"))

        assert result.used_model == "lexical-fallback"
        assert result.error == "SPARKSTATION_UNAVAILABLE"
        assert len(result.results) == 2
        # "match.md" should rank higher (has "python")
        assert result.results[0].path == "match.md"
        assert result.results[0].score > result.results[1].score

    @patch("kavi.skills.search_notes.embed")
    def test_fallback_scores(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "full.md", "hello world")
        _write_note(tmp_path, "partial.md", "hello planet")
        _write_note(tmp_path, "none.md", "nothing here")
        mock_embed.side_effect = SparkUnavailableError("down")

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="hello world"))

        scores = {r.path: r.score for r in result.results}
        assert scores["full.md"] == 1.0
        assert scores["partial.md"] == 0.5
        assert scores["none.md"] == 0.0


# ---------------------------------------------------------------------------
# Tag filter
# ---------------------------------------------------------------------------


class TestTagFilter:
    @patch("kavi.skills.search_notes.embed")
    def test_tag_filter(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "tagged.md", "# Tagged\n\nContent #work")
        _write_note(tmp_path, "untagged.md", "# Other\n\nContent here")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="content", tag="work"))

        assert len(result.results) == 1
        assert result.results[0].path == "tagged.md"

    @patch("kavi.skills.search_notes.embed")
    def test_tag_with_hash_prefix(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "tagged.md", "# Tagged\n\n#project content")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="content", tag="#project"))

        assert len(result.results) == 1

    @patch("kavi.skills.search_notes.embed")
    def test_empty_tag_ignored(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# Note\n\nContent")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="content", tag=""))

        assert len(result.results) == 1

    @patch("kavi.skills.search_notes.embed")
    def test_tag_no_matches(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# Note\n\nContent without tags")

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="content", tag="nonexistent"))

        assert len(result.results) == 0
        mock_embed.assert_not_called()


# ---------------------------------------------------------------------------
# Nested directories & symlinks
# ---------------------------------------------------------------------------


class TestFileHandling:
    @patch("kavi.skills.search_notes.embed")
    def test_nested_notes(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "daily/2025-01-01.md", "# New Year\n\nCelebration")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="celebration"))

        assert len(result.results) == 1
        assert result.results[0].path == "daily/2025-01-01.md"

    @patch("kavi.skills.search_notes.embed")
    def test_symlinks_skipped(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        real = tmp_path / "secret.md"
        real.write_text("secret content")
        link = vault / "link.md"
        link.symlink_to(real)

        _write_note(tmp_path, "real.md", "# Real\n\nActual content")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.execute(SearchNotesInput(query="content"))

        paths = [r.path for r in result.results]
        assert "link.md" not in paths
        assert "real.md" in paths


# ---------------------------------------------------------------------------
# validate_and_run integration
# ---------------------------------------------------------------------------


class TestValidateAndRun:
    @patch("kavi.skills.search_notes.embed")
    def test_validate_and_run(self, mock_embed: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "test.md", "# Test\n\nSome test content")
        mock_embed.return_value = [[1.0], [0.9]]

        skill = SearchNotesSkill()
        result = skill.validate_and_run({"query": "test"})

        assert result["query"] == "test"
        assert len(result["results"]) == 1
        assert result["results"][0]["path"] == "test.md"
        assert result["error"] is None
