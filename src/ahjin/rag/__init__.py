"""RAG subsystem exports."""

from ahjin.rag.base import BaseRagEngine, RetrievalChunk, RetrievalContext
from ahjin.rag.chunker import ChunkDescriptor, TextChunker
from ahjin.rag.embedding import (
    BaseEmbeddingService,
    EmbeddingRequest,
    EmbeddingResponse,
    OllamaEmbeddingService,
)
from ahjin.rag.engine import RagEngine
from ahjin.rag.ingestor import DocumentIngestor, ExtractedDocument, ExtractedPage
from ahjin.rag.retriever import SemanticRetriever
from ahjin.rag.vector_store import ScoredChunk, SQLiteVectorStore

__all__ = [
    "BaseRagEngine",
    "RetrievalChunk",
    "RetrievalContext",
    "BaseEmbeddingService",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "OllamaEmbeddingService",
    "DocumentIngestor",
    "ExtractedDocument",
    "ExtractedPage",
    "ChunkDescriptor",
    "TextChunker",
    "SQLiteVectorStore",
    "ScoredChunk",
    "SemanticRetriever",
    "RagEngine",
]
