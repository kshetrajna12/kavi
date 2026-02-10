"""Pydantic models for the agent layer: intents, plans, and responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kavi.consumer.chain import ChainSpec
from kavi.consumer.shim import ExecutionRecord

# ── Session context (D015) ────────────────────────────────────────────

MAX_ANCHORS = 10


class Anchor(BaseModel):
    """A referenceable artifact from a prior turn."""

    kind: Literal["execution", "artifact"] = "execution"
    label: str
    execution_id: str
    skill_name: str
    data: dict[str, Any] = Field(default_factory=dict)


# Curated data extractors per skill — keeps anchors small and useful.
_ANCHOR_EXTRACTORS: dict[str, list[str]] = {
    "search_notes": ["query"],
    "summarize_note": ["path", "summary"],
    "write_note": ["written_path"],
    "read_notes_by_tag": ["tag", "count"],
    "http_get_json": ["url"],
    "create_daily_note": ["path", "date"],
}


def _extract_anchor_data(skill_name: str, output: dict[str, Any]) -> dict[str, Any]:
    """Extract curated subset of output for an anchor."""
    keys = _ANCHOR_EXTRACTORS.get(skill_name)
    if keys is None:
        # Unknown skill — take top-level scalar fields (up to 5)
        result: dict[str, Any] = {}
        for k, v in output.items():
            if isinstance(v, (str, int, float, bool)) and len(result) < 5:
                result[k] = v
        return result
    data: dict[str, Any] = {}
    for key in keys:
        if key in output:
            data[key] = output[key]
    # Special case: search_notes top result path
    if skill_name == "search_notes":
        results = output.get("results", [])
        if results and isinstance(results, list) and len(results) > 0:
            top = results[0]
            if isinstance(top, dict) and "path" in top:
                data["top_result_path"] = top["path"]
    return data


class SessionContext(BaseModel):
    """Sliding window of referenceable anchors from prior turns (D015)."""

    anchors: list[Anchor] = Field(default_factory=list)

    def add_from_records(self, records: list[ExecutionRecord]) -> None:
        """Extract anchors from execution records and append."""
        for rec in records:
            if not rec.success or rec.output_json is None:
                continue
            data = _extract_anchor_data(rec.skill_name, rec.output_json)
            anchor = Anchor(
                label=f"{rec.skill_name} result",
                execution_id=rec.execution_id,
                skill_name=rec.skill_name,
                data=data,
            )
            self.anchors.append(anchor)
        # Enforce sliding window
        if len(self.anchors) > MAX_ANCHORS:
            self.anchors = self.anchors[-MAX_ANCHORS:]

    def resolve(self, ref: str) -> Anchor | None:
        """Resolve a ref string to a single anchor.

        Patterns:
        - "last" / "that" / "it" → most recent anchor
        - "last_<skill>" → most recent anchor for skill
        - "exec:<id_prefix>" → anchor by execution ID prefix
        """
        if not self.anchors:
            return None

        ref_lower = ref.lower().strip()

        if ref_lower in ("last", "that", "it", "the result"):
            return self.anchors[-1]

        if ref_lower.startswith("last_"):
            skill_suffix = ref_lower[5:]  # after "last_"
            # Priority: exact > startswith > contains
            # Check each tier fully before falling through
            for anchor in reversed(self.anchors):
                if anchor.skill_name == skill_suffix:
                    return anchor
            for anchor in reversed(self.anchors):
                if anchor.skill_name.startswith(skill_suffix):
                    return anchor
            for anchor in reversed(self.anchors):
                if skill_suffix in anchor.skill_name:
                    return anchor
            return None

        if ref_lower.startswith("exec:"):
            prefix = ref_lower[5:]
            for anchor in reversed(self.anchors):
                if anchor.execution_id.startswith(prefix):
                    return anchor
            return None

        return None

    def ambiguous(self, ref: str) -> list[Anchor]:
        """Return candidate anchors when ref could match multiple."""
        if not self.anchors:
            return []

        ref_lower = ref.lower().strip()

        if ref_lower.startswith("exec:"):
            prefix = ref_lower[5:]
            return [
                a for a in self.anchors
                if a.execution_id.startswith(prefix)
            ]

        return []


class AmbiguityResponse(BaseModel):
    """Returned when a ref cannot be unambiguously resolved."""

    ref: str
    candidates: list[Anchor]
    message: str

# ── Parsed intents (discriminated union) ─────────────────────────────


class SearchAndSummarizeIntent(BaseModel):
    kind: Literal["search_and_summarize"] = "search_and_summarize"
    query: str
    top_k: int = 5
    style: Literal["bullet", "paragraph"] = "bullet"


class WriteNoteIntent(BaseModel):
    kind: Literal["write_note"] = "write_note"
    title: str
    body: str


class SkillInvocationIntent(BaseModel):
    """Generic intent for any registered skill — no custom wiring needed."""

    kind: Literal["skill_invocation"] = "skill_invocation"
    skill_name: str
    input: dict[str, Any] = Field(default_factory=dict)


class TransformIntent(BaseModel):
    """Refine/correct the last execution with field overrides.

    LLM proposes overrides; runtime binds target deterministically
    from session anchors. Resolver converts to SkillInvocationIntent.
    """

    kind: Literal["transform"] = "transform"
    overrides: dict[str, Any] = Field(default_factory=dict)
    target_ref: str = "last"


class HelpIntent(BaseModel):
    kind: Literal["help"] = "help"


class UnsupportedIntent(BaseModel):
    kind: Literal["unsupported"] = "unsupported"
    message: str


ParsedIntent = (
    SearchAndSummarizeIntent
    | WriteNoteIntent
    | SkillInvocationIntent
    | TransformIntent
    | HelpIntent
    | UnsupportedIntent
)


# ── Conventions ──────────────────────────────────────────────────────

NOTE_PATH_PREFIX = "Inbox/AI"


def note_path_for_title(title: str) -> str:
    """Build the canonical vault path for a generated note."""
    return f"{NOTE_PATH_PREFIX}/{title}.md"


# ── Planned actions ──────────────────────────────────────────────────


class SkillAction(BaseModel):
    kind: Literal["skill"] = "skill"
    skill_name: str
    input: dict[str, Any]


class ChainAction(BaseModel):
    kind: Literal["chain"] = "chain"
    chain: ChainSpec


PlannedAction = SkillAction | ChainAction


# ── Agent response ───────────────────────────────────────────────────


class AgentResponse(BaseModel):
    """Top-level response from AgentCore.handle_message.

    Always populated — on error, `error` is set and `records` is empty.
    ``warnings`` carries parser-generated notices when parts of the user
    request could not be fulfilled (e.g. trailing intents ignored).
    """

    intent: ParsedIntent
    plan: PlannedAction | None = None
    records: list[ExecutionRecord] = Field(default_factory=list)
    needs_confirmation: bool = False
    warnings: list[str] = Field(default_factory=list)
    help_text: str | None = None
    error: str | None = None
    session: SessionContext | None = None
