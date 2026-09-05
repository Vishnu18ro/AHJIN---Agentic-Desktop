"""ContextAssembler — Context construction boundary.

v1 implementation lives under Harness for simplicity.
This does not imply context construction is permanently owned by Harness at the architectural level.

ContextualizedPrompt is defined in ahjin.providers.types as it is a provider-boundary concept.
ContextAssembler constructs it; providers consume it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ahjin.beru.types import ModelStepIntent
from ahjin.core.types import TaskContext
from ahjin.providers.types import ContextualizedPrompt

if TYPE_CHECKING:
    from ahjin.harness.state import StepResult
    from ahjin.memory.base import MemoryContext
    from ahjin.rag.base import RetrievalContext

__all__ = ["ContextAssembler", "ContextualizedPrompt"]


class ContextAssembler:
    """Assembles prompt content from context sources."""

    def assemble(
        self,
        intent: ModelStepIntent,
        task_context: TaskContext,
        memory: "MemoryContext | None" = None,
        retrieval: "RetrievalContext | None" = None,
        prior_results: list["StepResult"] | None = None,
    ) -> ContextualizedPrompt:
        user_instruction = intent.instruction
        if prior_results:
            result_blocks: list[str] = []
            for res in prior_results:
                output_content = (
                    res.output_text
                    if res.output_text is not None
                    else (str(res.error) if res.error else "No output")
                )
                success_str = "true" if res.success else "false"
                block = (
                    f"[TOOL RESULTS]\n"
                    f"Step: {res.step_id}\n"
                    f"Success: {success_str}\n"
                    f"Output:\n"
                    f"{output_content}\n"
                    f"[/TOOL RESULTS]"
                )
                result_blocks.append(block)
            if result_blocks:
                grounding_note = (
                    "INSTRUCTION TO MODEL: Base your response strictly on the tool observation "
                    "results above. If web search results are provided, answer using those "
                    "retrieved sources and cite relevant source URLs or domains. If a search "
                    "returned no results or failed, state clearly that search did not return "
                    "results and do not invent information. For local files, clearly "
                    "distinguish actual user files from source/test files."
                )
                user_instruction = (
                    user_instruction + "\n\n" + "\n\n".join(result_blocks) + "\n\n" + grounding_note
                )

        return ContextualizedPrompt(
            conversation_history=task_context.conversation_history,
            user_instruction=user_instruction,
        )

