"""Skill: search_notes — Semantic search over vault markdown notes."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath

from pydantic import Field

from kavi.config import SPARK_EMBED_MODEL
from kavi.llm.spark import SparkUnavailableError, embed
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

VAULT_OUT = Path("vault_out")

_SNIPPET_CHARS = 200


# ---------------------------------------------------------------------------
# I/O models
# ---------------------------------------------------------------------------


class SearchNotesInput(SkillInput):
    """Input for search_notes skill."""

    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    max_chars: int = Field(default=12000, ge=1)
    timeout_s: float = Field(default=8.0, gt=0)
    include_snippet: bool = True
    tag: str | None = None


class SearchResult(SkillOutput):
    """A single search result."""

    path: str
    score: float
    title: str | None = None
    snippet: str | None = None


class SearchNotesOutput(SkillOutput):
    """Output for search_notes skill."""

    query: str
    results: list[SearchResult]
    truncated_paths: list[str]
    used_model: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(content: str) -> str | None:
    """Return the first H1 heading, or None."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _has_tag(content: str, tag: str) -> bool:
    """Check whether *content* contains #tag as a standalone tag."""
    needle = f"#{tag}"
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# ") or stripped == "#":
            continue
        idx = 0
        while True:
            pos = line.find(needle, idx)
            if pos == -1:
                break
            end = pos + len(needle)
            if end < len(line) and line[end].isalnum():
                idx = end
                continue
            return True
    return False


def _snippet(content: str, query: str, length: int = _SNIPPET_CHARS) -> str:
    """Return a snippet around the first occurrence of *query*, or the start."""
    lower = content.lower()
    q_lower = query.lower()
    pos = lower.find(q_lower)
    if pos == -1:
        return content[:length].strip()
    start = max(0, pos - length // 4)
    return content[start : start + length].strip()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _lexical_score(content: str, query: str) -> float:
    """Simple lexical ranking: fraction of query tokens found in content."""
    tokens = query.lower().split()
    if not tokens:
        return 0.0
    lower = content.lower()
    return sum(1 for t in tokens if t in lower) / len(tokens)


# ---------------------------------------------------------------------------
# Vault enumeration
# ---------------------------------------------------------------------------


def _enumerate_notes(
    max_chars: int,
    tag: str | None,
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """Read vault notes, return (entries, truncated_paths).

    Each entry is (relative_path, content, title).
    """
    entries: list[tuple[str, str, str | None]] = []
    truncated: list[str] = []

    if not VAULT_OUT.exists():
        return entries, truncated

    for md_file in sorted(VAULT_OUT.rglob("*.md")):
        # Skip symlinks
        if md_file.is_symlink():
            continue

        # Reject paths with traversal
        rel = PurePosixPath(md_file.relative_to(VAULT_OUT))
        if ".." in rel.parts:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Tag filter
        if tag and not _has_tag(content, tag):
            continue

        was_truncated = len(content) > max_chars
        if was_truncated:
            content = content[:max_chars]
            truncated.append(str(rel))

        title = _extract_title(content)
        entries.append((str(rel), content, title))

    return entries, truncated


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


class SearchNotesSkill(BaseSkill):
    """Semantic search over vault markdown notes using Sparkstation embeddings."""

    name = "search_notes"
    description = (
        "Semantic search over vault markdown notes using Sparkstation bge-large "
        "embeddings with lexical fallback"
    )
    input_model = SearchNotesInput
    output_model = SearchNotesOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: SearchNotesInput) -> SearchNotesOutput:  # type: ignore[override]
        query = input_data.query.strip()
        if not query:
            return SearchNotesOutput(
                query=input_data.query,
                results=[],
                truncated_paths=[],
                used_model="none",
                error="EMPTY_QUERY",
            )

        tag = input_data.tag.strip().lstrip("#") if input_data.tag else None
        if tag == "":
            tag = None

        entries, truncated_paths = _enumerate_notes(input_data.max_chars, tag)

        if not entries:
            return SearchNotesOutput(
                query=input_data.query,
                results=[],
                truncated_paths=[],
                used_model=SPARK_EMBED_MODEL,
            )

        # Try semantic search via Sparkstation embeddings
        error: str | None = None
        try:
            scored = self._semantic_rank(query, entries, input_data.timeout_s)
            used_model = SPARK_EMBED_MODEL
        except SparkUnavailableError:
            scored = self._lexical_rank(query, entries)
            used_model = "lexical-fallback"
            error = "SPARKSTATION_UNAVAILABLE"

        # Sort descending by score, take top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: input_data.top_k]

        results: list[SearchResult] = []
        for score, path, content, title in top:
            snip = _snippet(content, query) if input_data.include_snippet else None
            results.append(
                SearchResult(path=path, score=round(score, 4), title=title, snippet=snip)
            )

        return SearchNotesOutput(
            query=input_data.query,
            results=results,
            truncated_paths=truncated_paths,
            used_model=used_model,
            error=error,
        )

    def _semantic_rank(
        self,
        query: str,
        entries: list[tuple[str, str, str | None]],
        timeout: float,
    ) -> list[tuple[float, str, str, str | None]]:
        """Rank entries by cosine similarity of bge-large embeddings."""
        texts = [content for _, content, _ in entries]
        all_texts = [query] + texts
        vectors = embed(all_texts, timeout=timeout)
        query_vec = vectors[0]

        scored: list[tuple[float, str, str, str | None]] = []
        for i, (path, content, title) in enumerate(entries):
            sim = _cosine_similarity(query_vec, vectors[i + 1])
            scored.append((sim, path, content, title))
        return scored

    def _lexical_rank(
        self,
        query: str,
        entries: list[tuple[str, str, str | None]],
    ) -> list[tuple[float, str, str, str | None]]:
        """Rank entries by lexical substring match."""
        scored: list[tuple[float, str, str, str | None]] = []
        for path, content, title in entries:
            score = _lexical_score(content, query)
            scored.append((score, path, content, title))
        return scored
