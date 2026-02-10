"""Skill: create_daily_note — Create or append a timestamped entry to today's daily note."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

VAULT_OUT = Path("vault_out")
DAILY_DIR = "daily"


class CreateDailyNoteInput(SkillInput):
    """Input for create_daily_note skill."""

    content: str


class CreateDailyNoteOutput(SkillOutput):
    """Output for create_daily_note skill."""

    path: str
    date: str
    sha256: str


class CreateDailyNoteSkill(BaseSkill):
    """Create or append a timestamped entry to today's daily note in the vault."""

    name = "create_daily_note"
    description = "Create or append a timestamped entry to today's daily note in the vault"
    input_model = CreateDailyNoteInput
    output_model = CreateDailyNoteOutput
    side_effect_class = "FILE_WRITE"

    def execute(self, input_data: CreateDailyNoteInput) -> CreateDailyNoteOutput:  # type: ignore[override]
        now = datetime.now(tz=UTC)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        filename = f"{date_str}.md"
        dest = VAULT_OUT / DAILY_DIR / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        entry = f"- {time_str} — {input_data.content}\n"

        if dest.exists():
            dest.write_text(dest.read_text() + entry)
        else:
            header = f"# {date_str}\n\n"
            dest.write_text(header + entry)

        content_bytes = dest.read_bytes()
        sha = hashlib.sha256(content_bytes).hexdigest()

        return CreateDailyNoteOutput(
            path=str(dest),
            date=date_str,
            sha256=sha,
        )
