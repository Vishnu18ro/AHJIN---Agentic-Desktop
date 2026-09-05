"""Unit tests for OllamaEmbeddingService."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ahjin.rag.embedding import EmbeddingRequest, OllamaEmbeddingService


def test_ollama_embedding_service_init_defaults() -> None:
    """Verify service initialization properties."""
    service = OllamaEmbeddingService(
        base_url="http://localhost:11434/v1", embedding_model="bge-m3:latest"
    )
    assert service.base_url == "http://localhost:11434/v1"
    assert service.embedding_model == "bge-m3:latest"


@pytest.mark.asyncio
async def test_ollama_embedding_service_single_string_embed() -> None:
    """Verify successful embedding generation for a single string input."""
    service = OllamaEmbeddingService(base_url="http://localhost:11434/v1")

    dummy_vector = [0.1] * 1024
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "data": [{"embedding": dummy_vector, "index": 0}],
        "usage": {"prompt_tokens": 10},
    }

    request = EmbeddingRequest(
        input_text="AHJIN is an agentic desktop intelligence system.",
        model_id="bge-m3:latest",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        response = await service.embed(request)

        assert response.dimension == 1024
        assert len(response.embeddings) == 1
        assert response.embeddings[0] == dummy_vector
        assert response.model_id == "bge-m3:latest"
        assert response.prompt_tokens == 10

        call_url = mock_post.call_args[0][0]
        call_json = mock_post.call_args[1]["json"]
        assert call_url == "http://localhost:11434/v1/embeddings"
        assert call_json["model"] == "bge-m3:latest"
        assert call_json["input"] == ["AHJIN is an agentic desktop intelligence system."]


@pytest.mark.asyncio
async def test_ollama_embedding_service_batch_list_embed() -> None:
    """Verify successful embedding generation for a list of strings."""
    service = OllamaEmbeddingService(base_url="http://localhost:11434/v1")

    vec1 = [0.1] * 1024
    vec2 = [0.2] * 1024
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"embedding": vec1, "index": 0},
            {"embedding": vec2, "index": 1},
        ],
        "usage": {"prompt_tokens": 20},
    }

    request = EmbeddingRequest(
        input_text=["First sentence", "Second sentence"],
        model_id="bge-m3:latest",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        response = await service.embed(request)

        assert response.dimension == 1024
        assert len(response.embeddings) == 2
        assert response.embeddings[0] == vec1
        assert response.embeddings[1] == vec2


@pytest.mark.asyncio
async def test_ollama_embedding_service_empty_embeddings_raises() -> None:
    """Verify ValueError is raised when returned embeddings are empty."""
    service = OllamaEmbeddingService(base_url="http://localhost:11434/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": []}

    request = EmbeddingRequest(input_text="Test string")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(ValueError, match="returned empty embeddings"):
            await service.embed(request)


@pytest.mark.asyncio
async def test_ollama_embedding_service_http_status_error() -> None:
    """Verify HTTPStatusError is propagated."""
    service = OllamaEmbeddingService(base_url="http://localhost:11434/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Server Error", request=MagicMock(), response=MagicMock(status_code=500)
    )

    request = EmbeddingRequest(input_text="Test string")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await service.embed(request)
