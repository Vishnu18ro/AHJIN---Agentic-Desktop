"""Unit tests for RAG pipeline components (Ingestor, Chunker, VectorStore, Retriever, RagEngine)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ahjin.rag.chunker import ChunkDescriptor, TextChunker
from ahjin.rag.embedding import EmbeddingResponse, OllamaEmbeddingService
from ahjin.rag.engine import RagEngine
from ahjin.rag.ingestor import DocumentIngestor, ExtractedDocument, ExtractedPage
from ahjin.rag.retriever import SemanticRetriever
from ahjin.rag.vector_store import SQLiteVectorStore


def test_ingestor_plain_text() -> None:
    """Ingestor converts plain text into a 1-page document representation."""
    ingestor = DocumentIngestor()
    doc = ingestor.ingest_text("Hello world AHJIN system", document_name="test.txt")
    assert doc.document_name == "test.txt"
    assert len(doc.pages) == 1
    assert doc.pages[0].page_number == 1
    assert doc.pages[0].content == "Hello world AHJIN system"


def test_ingestor_empty_text_raises() -> None:
    """Ingestor raises ValueError on empty text input."""
    ingestor = DocumentIngestor()
    with pytest.raises(ValueError, match="empty"):
        ingestor.ingest_text("   ", document_name="empty.txt")


def test_text_chunker_sliding_window_page_preservation() -> None:
    """Chunker splits pages into target word windows and preserves page numbers."""
    chunker = TextChunker(target_words=10, overlap_words=2)
    doc = ExtractedDocument(
        document_id="doc1",
        document_name="test_doc.pdf",
        pages=[
            ExtractedPage(page_number=1, content="word1 word2 word3 word4 word5 word6 word7 word8"),
            ExtractedPage(page_number=2, content="word9 word10 word11 word12 word13 word14"),
        ],
    )

    chunks = chunker.chunk_document(doc)
    assert len(chunks) >= 2
    assert chunks[0].document_id == "doc1"
    assert chunks[0].document_name == "test_doc.pdf"
    assert 1 in chunks[0].page_numbers
    assert chunks[1].chunk_index == 1


def test_sqlite_vector_store_persistence_and_search(tmp_path: Path) -> None:
    """SQLiteVectorStore persists chunks/embeddings and performs top-k search."""
    db_file = tmp_path / "test_rag.db"
    store = SQLiteVectorStore(db_path=db_file)

    c1 = ChunkDescriptor(
        document_id="doc1",
        document_name="doc1.pdf",
        chunk_index=0,
        page_numbers=[1],
        content="AHJIN is an agentic AI operating system.",
    )
    c2 = ChunkDescriptor(
        document_id="doc1",
        document_name="doc1.pdf",
        chunk_index=1,
        page_numbers=[2],
        content="BGE-M3 generates 1024-dimensional embeddings.",
    )

    v1 = [1.0] + [0.0] * 1023
    v2 = [0.0, 1.0] + [0.0] * 1022

    store.add_chunks([c1, c2], [v1, v2])

    q1 = [1.0] + [0.0] * 1023
    results = store.search(q1, top_k=1)

    assert len(results) == 1
    assert results[0].chunk.chunk_id == c1.chunk_id
    assert results[0].score > 0.8

    store2 = SQLiteVectorStore(db_path=db_file)
    results2 = store2.search(v2, top_k=1)
    assert len(results2) == 1
    assert results2[0].chunk.chunk_id == c2.chunk_id


@pytest.mark.asyncio
async def test_semantic_retriever_integration() -> None:
    """Retriever embeds query via service and searches vector store."""
    dummy_vec = [1.0] + [0.0] * 1023
    mock_service = AsyncMock(spec=OllamaEmbeddingService)
    mock_service.embed.return_value = EmbeddingResponse(
        embeddings=[dummy_vec],
        model_id="bge-m3:latest",
        dimension=1024,
    )

    mock_store = SQLiteVectorStore(db_path=":memory:")
    c1 = ChunkDescriptor(
        document_id="doc1",
        document_name="doc1.pdf",
        chunk_index=0,
        page_numbers=[3],
        content="Target chunk content",
    )
    mock_store.add_chunks([c1], [dummy_vec])

    retriever = SemanticRetriever(
        embedding_service=mock_service, vector_store=mock_store, default_top_k=5
    )

    ctx = await retriever.retrieve_chunks("What is target?")
    assert len(ctx.chunks) == 1
    assert ctx.chunks[0].content == "Target chunk content"
    assert "doc1.pdf#page=3" in ctx.chunks[0].source_uri
    assert ctx.chunks[0].score > 0.99


@pytest.mark.asyncio
async def test_rag_engine_grounded_prompt_construction() -> None:
    """RagEngine constructs grounded LLM prompt with context and anti-hallucination warning."""
    mock_retriever = AsyncMock()
    mock_retriever.retrieve_chunks.return_value = AsyncMock()

    engine = RagEngine(retriever=mock_retriever)
    ctx = await engine.retrieve("test query")
    mock_retriever.retrieve_chunks.assert_called_once_with("test query")

    empty_prompt = engine.build_grounded_prompt("Unknown fact?", ctx)
    assert "not available in the retrieved documents" in empty_prompt
