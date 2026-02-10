"""Pydantic models for the agent layer: intents, plans, and responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kavi.consumer.chain import ChainSpec
from kavi.consumer.shim import ExecutionRecord

# ── Parsed intents (discriminated union) ─────────────────────────────


class SearchAndSummarizeIntent(BaseModel):
    kind: Literal["search_and_summarize"] = "search_and_summarize"
    query: str
    top_k: int = 5
    style: Literal["bullet", "paragraph"] = "bullet"


class SummarizeNoteIntent(BaseModel):
    kind: Literal["summarize_note"] = "summarize_note"
    path: str
    style: Literal["bullet", "paragraph"] = "bullet"


class WriteNoteIntent(BaseModel):
    kind: Literal["write_note"] = "write_note"
    title: str
    body: str


class UnsupportedIntent(BaseModel):
    kind: Literal["unsupported"] = "unsupported"
    message: str


ParsedIntent = (
    SearchAndSummarizeIntent
    | SummarizeNoteIntent
    | WriteNoteIntent
    | UnsupportedIntent
)


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
    error: str | None = None
