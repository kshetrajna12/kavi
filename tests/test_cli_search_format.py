"""Tests for REPL search result formatting."""

from __future__ import annotations

from kavi.cli import format_search_results


class TestFormatSearchResults:
    def test_empty_results(self) -> None:
        out = format_search_results({"results": []})
        assert "No results" in out

    def test_missing_results_key(self) -> None:
        out = format_search_results({})
        assert "No results" in out

    def test_basic_table(self) -> None:
        output_json = {
            "results": [
                {"path": "notes/a.md", "score": 0.9123, "title": "Alpha", "snippet": "..."},
                {"path": "notes/b.md", "score": 0.7001, "title": None, "snippet": "..."},
            ],
        }
        out = format_search_results(output_json)
        lines = out.splitlines()
        # Header + separator + 2 result rows
        assert len(lines) >= 4
        # Check rank numbering
        assert "1" in lines[2]
        assert "2" in lines[3]
        # Check score formatting (4 decimals)
        assert "0.9123" in out
        assert "0.7001" in out
        # Check path
        assert "notes/a.md" in out
        assert "notes/b.md" in out
        # Check title present for first, absent for second
        assert "Alpha" in out

    def test_no_snippet_by_default(self) -> None:
        output_json = {
            "results": [
                {"path": "a.md", "score": 0.5, "title": "X", "snippet": "SHOULD NOT APPEAR"},
            ],
        }
        out = format_search_results(output_json, verbose=False)
        assert "SHOULD NOT APPEAR" not in out

    def test_verbose_shows_top_snippet(self) -> None:
        output_json = {
            "results": [
                {"path": "a.md", "score": 0.9, "title": "X", "snippet": "The snippet text"},
                {"path": "b.md", "score": 0.5, "title": "Y", "snippet": "Other snippet"},
            ],
        }
        out = format_search_results(output_json, verbose=True)
        assert "The snippet text" in out
        # Only top result snippet, not the second
        assert "Other snippet" not in out

    def test_verbose_no_snippet_field(self) -> None:
        output_json = {
            "results": [
                {"path": "a.md", "score": 0.9, "title": "X"},
            ],
        }
        # Should not crash when snippet is missing
        out = format_search_results(output_json, verbose=True)
        assert "a.md" in out

    def test_score_rounds_to_four_decimals(self) -> None:
        output_json = {
            "results": [
                {"path": "a.md", "score": 0.123456789, "title": None},
            ],
        }
        out = format_search_results(output_json)
        assert "0.1235" in out

    def test_snippet_bounded(self) -> None:
        long_snippet = "x" * 500
        output_json = {
            "results": [
                {"path": "a.md", "score": 0.9, "title": "T", "snippet": long_snippet},
            ],
        }
        out = format_search_results(output_json, verbose=True)
        # Snippet display bounded to 200 chars
        assert len(long_snippet) > 200
        assert "x" * 201 not in out
