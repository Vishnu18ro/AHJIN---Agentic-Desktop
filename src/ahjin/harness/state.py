"""Harness execution state tracking."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from ahjin.core.errors import AhjinError


class StepResult(BaseModel):
    """Result of a single PlanStep execution."""

    step_id: UUID
    success: bool
    output_text: str | None = None
    error: AhjinError | None = None
    attachment_paths: list[Path] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


class ExecutionState(BaseModel):
    """State of an ExecutionPlan execution."""

    task_id: UUID
    plan_id: UUID
    step_results: list[StepResult] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
