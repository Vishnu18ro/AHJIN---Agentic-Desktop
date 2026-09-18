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
            # Check if a subsequent file_read step succeeded with content
            has_successful_file_read = any(
                (
                    getattr(r, "tool_name", None) == "file_read"
                    or (
                        r.output_text is not None
                        and ("--- Content of " in r.output_text or "--- Page " in r.output_text)
                    )
                )
                and r.success
                and bool(r.output_text)
                for r in prior_results
            )

            result_blocks: list[str] = []
            for res in prior_results:
                output_content = (
                    res.output_text
                    if res.output_text is not None
                    else (str(res.error) if res.error else "No output")
                )

                # Objective C: Intermediate file-search context reduction
                # When file_read has already successfully retrieved the actual document content,
                # prune the raw multi-record search dump (which can contain 50+ file paths)
                # from the final model context to avoid bloating prompt and reasoning time.
                is_file_search = (
                    getattr(res, "tool_name", None) == "file_search"
                    or (
                        res.output_text is not None
                        and "match(es) for query '" in res.output_text
                    )
                )
                if has_successful_file_read and is_file_search and res.success:
                    first_line = (
                        output_content.splitlines()[0]
                        if output_content
                        else "File search completed."
                    )
                    output_content = (
                        f"{first_line}\n"
                        "Candidate document identified and read in subsequent step."
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
                    "AUTHORITATIVE SYSTEM CONSTRAINTS:\n"
                    "1. All [TOOL RESULTS] above are real, verified, and authoritative executions "
                    "on this computer.\n"
                    "2. You have authorized filesystem access through AHJIN. NEVER state "
                    "'I cannot access your filesystem', 'I don't have access to your computer', "
                    "or 'I cannot send files'. Doing so is strictly false.\n"
                    "3. If files were retrieved, read, or sent above, acknowledge the action "
                    "accurately and base your response strictly on the tool observation results.\n"
                    "4. If web search results are provided, answer using those retrieved sources "
                    "and cite relevant source URLs or domains. If a search returned no results "
                    "or failed, state clearly that search did not return results and do not "
                    "invent information."
                )
                user_instruction = (
                    user_instruction + "\n\n" + "\n\n".join(result_blocks) + "\n\n" + grounding_note
                )

        system_instruction = (
            "You are AHJIN 2.0, an Agentic AI Operating Layer. "
            "When greeting the user or when explicitly asked about your identity, identify yourself as AHJIN 2.0. "
            "Respond naturally and adaptively: briefly acknowledge your identity and, when useful, describe relevant "
            "capabilities or how you can help. Do not use a fixed greeting, fixed capability list, or repetitive "
            "self-introduction. Let the response wording, length, and emphasis vary naturally according to the "
            "model and conversation."
        )
        if prior_results:
            system_instruction = (
                "You are AHJIN 2.0, an Agentic AI Operating Layer with authorized local tool "
                "execution. You have full authorized access to local files and system tools "
                "on this machine. Never claim you cannot access the filesystem, browse local "
                "files, or send files."
            )

        return ContextualizedPrompt(
            system_instruction=system_instruction,
            conversation_history=task_context.conversation_history,
            user_instruction=user_instruction,
        )

