"""Skill: write_note — Write a markdown note to the vault."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

VAULT_OUT = Path("vault_out")


class WriteNoteInput(SkillInput):
    """Input for write_note skill."""

    path: str  # relative path under vault_out
    title: str
    body: str


class WriteNoteOutput(SkillOutput):
    """Output for write_note skill."""

    written_path: str  # full path of written file
    sha256: str  # SHA256 of file content


class WriteNoteSkill(BaseSkill):
    """Write a markdown note to the vault."""

    name = "write_note"
    description = "Write a markdown note to the vault"
    input_model = WriteNoteInput
    output_model = WriteNoteOutput
    side_effect_class = "FILE_WRITE"

    def execute(self, input_data: WriteNoteInput) -> WriteNoteOutput:  # type: ignore[override]
        rel = PurePosixPath(input_data.path)
        if rel.is_absolute() or ".." in rel.parts:
            msg = f"Invalid path: {input_data.path}"
            raise ValueError(msg)

        dest = VAULT_OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        content = f"# {input_data.title}\n\n{input_data.body}\n"
        content_bytes = content.encode()
        dest.write_bytes(content_bytes)

        sha = hashlib.sha256(content_bytes).hexdigest()

        return WriteNoteOutput(written_path=str(dest), sha256=sha)
