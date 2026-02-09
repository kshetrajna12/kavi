"""Base class for all Kavi skills."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class SkillInput(BaseModel):
    """Base class for skill input models. Override in concrete skills."""


class SkillOutput(BaseModel):
    """Base class for skill output models. Override in concrete skills."""


class BaseSkill(ABC):
    """Abstract base class for all Kavi skills.

    Every skill must define:
    - name: unique identifier
    - description: human-readable purpose
    - input_model / output_model: Pydantic classes for validation
    - side_effect_class: what kind of side effects this skill has
    - execute(): the actual logic
    """

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect_class: str
    required_secrets: list[str] = []

    @abstractmethod
    def execute(self, input_data: BaseModel) -> BaseModel:
        """Execute the skill with validated input. Returns validated output."""

    def validate_and_run(self, raw_input: dict[str, Any]) -> dict[str, Any]:
        """Validate input, execute, validate output, return dict."""
        validated_input = self.input_model(**raw_input)
        result = self.execute(validated_input)
        validated_output = self.output_model.model_validate(result.model_dump())
        return validated_output.model_dump()
