"""Semantic Retriever connecting embedding service and vector store."""

from ahjin.rag.base import RetrievalChunk, RetrievalContext
from ahjin.rag.embedding import BaseEmbeddingService, EmbeddingRequest
from ahjin.rag.vector_store import SQLiteVectorStore


class SemanticRetriever:
    """Semantic Retriever performing vector search using embedding service."""

    def __init__(
        self,
        embedding_service: BaseEmbeddingService,
        vector_store: SQLiteVectorStore,
        default_top_k: int = 5,
    ) -> None:
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.default_top_k = default_top_k

    async def retrieve_chunks(
        self, query: str, top_k: int | None = None
    ) -> RetrievalContext:
        """Embed user query and retrieve top_k scored chunks."""
        k = top_k if top_k is not None else self.default_top_k
        emb_resp = await self.embedding_service.embed(
            EmbeddingRequest(input_text=query)
        )

        query_vec = emb_resp.embeddings[0]
        scored_chunks = self.vector_store.search(query_vec, top_k=k)

        chunks: list[RetrievalChunk] = []
        for sc in scored_chunks:
            c = sc.chunk
            pages_str = ",".join(str(p) for p in c.page_numbers)
            source_uri = f"{c.document_name}#page={pages_str}"

            chunks.append(
                RetrievalChunk(
                    content=c.content,
                    source_uri=source_uri,
                    score=sc.score,
                )
            )

        return RetrievalContext(chunks=chunks)
