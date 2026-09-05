"""Unit tests for OllamaProvider."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ahjin.core.types import ConversationTurn, Role
from ahjin.models.catalog import create_default_catalog
from ahjin.models.types import ModelTier
from ahjin.providers.ollama import OllamaProvider
from ahjin.providers.types import (
    ContextualizedPrompt,
    FinishReason,
    ModelInvocationRequest,
)


def test_local_ollama_models_catalog_registration() -> None:
    """Verify local Ollama models (gemma3:4b, qwen3:8b) in default catalog."""
    catalog = create_default_catalog()

    gemma = catalog.get_model("gemma3:4b")
    assert gemma.provider_id == "ollama"
    assert gemma.tier == ModelTier.FAST
    assert gemma.priority == 100
    assert gemma.quality_score == 80

    qwen = catalog.get_model("qwen3:8b")
    assert qwen.provider_id == "ollama"
    assert qwen.tier == ModelTier.HEAVY
    assert qwen.priority == 120
    assert qwen.quality_score == 85

    # Verify cloud priorities remain untouched and higher than local
    fast_cloud = catalog.get_model("nvidia/nemotron-3.5-lightning-30b-a3b")
    assert fast_cloud.priority > gemma.priority

    heavy_cloud = catalog.get_model("minimax/minimax-m3:free")
    assert heavy_cloud.priority > qwen.priority


def test_ollama_provider_init_defaults() -> None:
    """Verify provider initialization properties."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1", default_model="gemma3:4b")
    assert provider.provider_id == "ollama"
    assert provider.get_default_model_id() == "gemma3:4b"
    assert provider.base_url == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_ollama_provider_successful_invocation() -> None:
    """Verify successful Ollama chat completion API invocation."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1", default_model="gemma3:4b")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello from Ollama local!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
    }

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(user_instruction="Hello"),
        model_id="gemma3:4b",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        response = await provider.invoke(request)

        assert response.content == "Hello from Ollama local!"
        assert response.model_id == "gemma3:4b"
        assert response.provider_id == "ollama"
        assert response.finish_reason == FinishReason.COMPLETE
        assert response.usage is not None
        assert response.usage.prompt_tokens == 15
        assert response.usage.completion_tokens == 8
        assert response.usage.total_tokens == 23

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        call_json = mock_post.call_args[1]["json"]

        assert call_url == "http://localhost:11434/v1/chat/completions"
        assert call_json["model"] == "gemma3:4b"
        assert len(call_json["messages"]) == 2  # system + user


@pytest.mark.asyncio
async def test_ollama_provider_payload_construction_with_history() -> None:
    """Verify system prompt, conversation history, and user instruction mapping."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}]
    }

    history = [
        ConversationTurn(role=Role.USER, content="What is 6x7?"),
        ConversationTurn(role=Role.ASSISTANT, content="42"),
    ]

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(
            system_instruction="You are a math bot.",
            conversation_history=history,
            user_instruction="Are you sure?",
        ),
        model_id="qwen3:8b",
        max_tokens=2048,
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        response = await provider.invoke(request)
        assert response.content == "42"

        call_json = mock_post.call_args[1]["json"]
        assert call_json["model"] == "qwen3:8b"
        assert call_json["max_tokens"] == 2048
        assert call_json["messages"] == [
            {"role": "system", "content": "You are a math bot."},
            {"role": "user", "content": "What is 6x7?"},
            {"role": "assistant", "content": "42"},
            {"role": "user", "content": "Are you sure?"},
        ]


@pytest.mark.asyncio
async def test_ollama_provider_empty_model_id_raises() -> None:
    """Invocation must raise ValueError if model_id is empty."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1")

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(user_instruction="Hi"),
        model_id="",
    )

    with pytest.raises(ValueError, match="model_id is not specified"):
        await provider.invoke(request)


@pytest.mark.asyncio
async def test_ollama_provider_empty_response_content_raises() -> None:
    """Invocation must raise ValueError if API returns empty message content."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {"content": ""}}]}

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(user_instruction="Hello"),
        model_id="gemma3:4b",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(ValueError, match="returned empty content"):
            await provider.invoke(request)


@pytest.mark.asyncio
async def test_ollama_provider_http_status_error() -> None:
    """Verify HTTP status errors are propagated properly."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=MagicMock(),
        response=MagicMock(status_code=500),
    )

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(user_instruction="Hello"),
        model_id="gemma3:4b",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await provider.invoke(request)


@pytest.mark.asyncio
async def test_ollama_provider_timeout_error() -> None:
    """Verify HTTP timeout errors are propagated properly."""
    provider = OllamaProvider(base_url="http://localhost:11434/v1")

    request = ModelInvocationRequest(
        prompt=ContextualizedPrompt(user_instruction="Hello"),
        model_id="gemma3:4b",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("Request timed out")

        with pytest.raises(httpx.TimeoutException):
            await provider.invoke(request)
