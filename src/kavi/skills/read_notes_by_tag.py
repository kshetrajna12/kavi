"""Skill: read_notes_by_tag — Read all notes matching a tag from the vault."""

from __future__ import annotations

from pathlib import Path

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

VAULT_OUT = Path("vault_out")


class NoteInfo(SkillOutput):
    """A single note entry."""

    path: str
    title: str


class ReadNotesByTagInput(SkillInput):
    """Input for read_notes_by_tag skill."""

    tag: str


class ReadNotesByTagOutput(SkillOutput):
    """Output for read_notes_by_tag skill."""

    notes: list[NoteInfo]
    count: int


class ReadNotesByTagSkill(BaseSkill):
    """Read all notes matching a tag from the vault."""

    name = "read_notes_by_tag"
    description = "Read all notes matching a tag from the vault"
    input_model = ReadNotesByTagInput
    output_model = ReadNotesByTagOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: ReadNotesByTagInput) -> ReadNotesByTagOutput:  # type: ignore[override]
        tag = input_data.tag.strip().lstrip("#")
        if not tag:
            return ReadNotesByTagOutput(notes=[], count=0)

        notes: list[NoteInfo] = []

        if not VAULT_OUT.exists():
            return ReadNotesByTagOutput(notes=[], count=0)

        for md_file in sorted(VAULT_OUT.rglob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            if _has_tag(content, tag):
                title = _extract_title(content, md_file.stem)
                rel_path = str(md_file.relative_to(VAULT_OUT))
                notes.append(NoteInfo(path=rel_path, title=title))

        return ReadNotesByTagOutput(notes=notes, count=len(notes))


def _has_tag(content: str, tag: str) -> bool:
    """Check whether *content* contains #tag as a standalone tag."""
    needle = f"#{tag}"
    for line in content.splitlines():
        # Skip markdown headings (lines starting with #)
        stripped = line.lstrip()
        if stripped.startswith("# ") or stripped == "#":
            continue
        idx = 0
        while True:
            pos = line.find(needle, idx)
            if pos == -1:
                break
            end = pos + len(needle)
            # Character after the tag must be whitespace, punctuation, or end-of-line
            if end < len(line) and line[end].isalnum():
                idx = end
                continue
            return True
    return False


def _extract_title(content: str, fallback: str) -> str:
    """Extract the first H1 heading from markdown content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback
