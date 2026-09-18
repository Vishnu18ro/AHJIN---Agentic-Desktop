"""Telegram Update ◄► TaskRequest / TaskResult mapper."""

from ahjin.core.types import (
    Modality,
    RequestMetadata,
    TaskContext,
    TaskRequest,
    TaskResult,
    UserIntent,
)


class TelegramMapper:
    """Translates Telegram data structures to canonical AHJIN domain types."""

    @staticmethod
    def to_task_request(
        chat_id: int,
        message_text: str,
        conversation_history: "list[dict[str, str]] | None" = None,
    ) -> TaskRequest:
        """Map Telegram message input to canonical TaskRequest.

        Args:
            chat_id: Telegram chat identifier.
            message_text: The raw user message text.
            conversation_history: Optional rolling history of prior turns as
                ``[{"role": "user"|"assistant", "content": "..."}]`` dicts.
                These are converted to ConversationTurn domain objects.
        """
        from ahjin.core.types import ConversationTurn, Role

        intent = UserIntent(
            primary_text=message_text,
            modality=Modality.TEXT,
        )

        formatted_history: list[ConversationTurn] = []
        if conversation_history:
            for msg in conversation_history:
                role_str = msg.get("role", "unknown").lower()
                content = msg.get("content", "")
                if role_str == "user":
                    role = Role.USER
                elif role_str == "assistant":
                    role = Role.ASSISTANT
                else:
                    role = Role.SYSTEM
                formatted_history.append(ConversationTurn(role=role, content=content))

        context = TaskContext(
            session_id=f"telegram:{chat_id}",
            conversation_history=formatted_history,
        )
        metadata = RequestMetadata(
            source_interface="telegram",
        )
        return TaskRequest(
            intent=intent,
            context=context,
            metadata=metadata,
        )

    @staticmethod
    def to_telegram_response(result: TaskResult) -> str:
        """Map TaskResult to Telegram response text."""
        if result.success and result.output_text:
            return result.output_text
        if result.error:
            return f"Error [{result.error.code}]: {result.error.message}"
        return "An unknown error occurred."
