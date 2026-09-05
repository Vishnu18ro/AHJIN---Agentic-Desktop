"""Document Ingestor for PDF and text file processing."""

from dataclasses import dataclass
from pathlib import Path

import pypdf


@dataclass
class ExtractedPage:
    """Extracted document page representation."""

    page_number: int  # 1-indexed
    content: str


@dataclass
class ExtractedDocument:
    """Extracted document with page-level text."""

    document_id: str
    document_name: str
    pages: list[ExtractedPage]


class DocumentIngestor:
    """Extracts page-level text from PDF documents and plain text files."""

    def ingest_pdf(
        self, file_path: str | Path, document_id: str | None = None
    ) -> ExtractedDocument:
        """Extract text from PDF file preserving 1-indexed page numbers."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        doc_id = document_id or path.stem
        doc_name = path.name

        reader = pypdf.PdfReader(str(path))
        pages: list[ExtractedPage] = []

        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(ExtractedPage(page_number=idx, content=text))

        if not pages:
            raise ValueError(f"No extractable text found in PDF: {doc_name}")

        return ExtractedDocument(document_id=doc_id, document_name=doc_name, pages=pages)

    def ingest_text(
        self,
        text_content: str,
        document_name: str = "document.txt",
        document_id: str | None = None,
    ) -> ExtractedDocument:
        """Ingest plain text as a 1-page document."""
        doc_id = document_id or document_name.split(".")[0]
        cleaned = text_content.strip()
        if not cleaned:
            raise ValueError(f"Text content is empty for document: {document_name}")
        pages = [ExtractedPage(page_number=1, content=cleaned)]
        return ExtractedDocument(document_id=doc_id, document_name=document_name, pages=pages)
