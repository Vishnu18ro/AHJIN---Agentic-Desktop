"""Deterministic Text Chunker for page-aware document splitting."""

from uuid import uuid4

from pydantic import BaseModel, Field

from ahjin.rag.ingestor import ExtractedDocument


class ChunkDescriptor(BaseModel):
    """Chunk of text with page and source metadata."""

    chunk_id: str = Field(default_factory=lambda: str(uuid4()))
    document_id: str
    document_name: str
    chunk_index: int
    page_numbers: list[int]
    content: str


class TextChunker:
    """Sliding-window text chunker for page-aware document splitting."""

    def __init__(self, target_words: int = 500, overlap_words: int = 50) -> None:
        self.target_words = target_words
        self.overlap_words = overlap_words

    def chunk_document(self, document: ExtractedDocument) -> list[ChunkDescriptor]:
        """Split document pages into overlapping word-based chunks preserving page metadata."""
        word_tokens: list[tuple[str, int]] = []
        for page in document.pages:
            words = page.content.split()
            for w in words:
                word_tokens.append((w, page.page_number))

        if not word_tokens:
            return []

        chunks: list[ChunkDescriptor] = []
        step = max(1, self.target_words - self.overlap_words)
        total_words = len(word_tokens)
        chunk_idx = 0

        for start in range(0, total_words, step):
            end = min(total_words, start + self.target_words)
            slice_tokens = word_tokens[start:end]

            chunk_text = " ".join(w for w, _ in slice_tokens)
            pages = sorted(list(dict.fromkeys(p for _, p in slice_tokens)))

            chunks.append(
                ChunkDescriptor(
                    document_id=document.document_id,
                    document_name=document.document_name,
                    chunk_index=chunk_idx,
                    page_numbers=pages,
                    content=chunk_text,
                )
            )
            chunk_idx += 1

            if end >= total_words:
                break

        return chunks
