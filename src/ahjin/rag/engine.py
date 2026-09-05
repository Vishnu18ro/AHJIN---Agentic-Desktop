"""RagEngine implementing BaseRagEngine with grounded context assembly."""

from ahjin.rag.base import BaseRagEngine, RetrievalContext
from ahjin.rag.retriever import SemanticRetriever


class RagEngine(BaseRagEngine):
    """Production RAG engine for knowledge retrieval and context assembly."""

    def __init__(self, retriever: SemanticRetriever) -> None:
        self.retriever = retriever

    async def retrieve(self, query: str) -> RetrievalContext:
        """Retrieve relevant document chunks for user query."""
        return await self.retriever.retrieve_chunks(query)

    def build_grounded_prompt(self, query: str, context: RetrievalContext) -> str:
        """Build grounded LLM prompt with context and anti-hallucination instruction."""
        if not context.chunks:
            return (
                f"The user asked: '{query}'\n\n"
                "Note: No relevant document context was found. If the answer relies on "
                "document knowledge, state that the information is not available in the "
                "retrieved documents."
            )

        context_blocks: list[str] = []
        for chunk in context.chunks:
            context_blocks.append(
                f"[Document Source: {chunk.source_uri} | Score: {chunk.score:.3f}]\n{chunk.content}"
            )

        joined_context = "\n\n".join(context_blocks)

        prompt = (
            "Use ONLY the following retrieved document context to answer the user's question. "
            "If the retrieved context is insufficient or does not contain the answer, "
            "explicitly state that the information is not available in the retrieved "
            "documents. Do NOT make up information.\n\n"
            "=== RETRIEVED CONTEXT ===\n"
            f"{joined_context}\n"
            "=========================\n\n"
            f"User Question: {query}\n"
        )
        return prompt
