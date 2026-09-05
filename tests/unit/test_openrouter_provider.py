"""Focused unit tests for OpenRouterProvider."""

import pytest

from ahjin.core.config import settings
from ahjin.providers.openrouter import OpenRouterProvider
from ahjin.providers.types import ContextualizedPrompt, ModelInvocationRequest


def test_openrouter_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouterProvider must raise ValueError if OPENROUTER_API_KEY is missing."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is not configured"):
        OpenRouterProvider()


def test_openrouter_provider_initialization() -> None:
    """OpenRouterProvider must initialize cleanly with configured credentials."""
    provider = OpenRouterProvider(
        api_key="test-openrouter-key", default_model="nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    assert provider.provider_id == "openrouter"
    assert provider.api_key == "test-openrouter-key"
    assert provider.get_default_model_id() == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert provider.timeout_seconds == 90.0



def test_openrouter_provider_requires_model_id() -> None:
    """OpenRouterProvider.invoke must raise ValueError if model_id is missing."""
    provider = OpenRouterProvider(api_key="test-key", default_model="")
    prompt = ContextualizedPrompt(user_instruction="Hello")
    request = ModelInvocationRequest(prompt=prompt, model_id="")

    with pytest.raises(ValueError, match="model_id is not specified"):
        import asyncio
        asyncio.run(provider.invoke(request))
