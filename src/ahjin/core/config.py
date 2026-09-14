"""Application settings via Pydantic Settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """AHJIN 2.0 configuration settings."""

    ahjin_env: str = "development"
    offline_mode: bool = False

    # Telegram
    telegram_bot_token: str = ""

    # NVIDIA Provider
    # No code defaults for credentials or model selection (ADR-003).
    # Empty string is the "not configured" sentinel.
    # NvidiaProvider validates these at construction and raises clearly if missing.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # max_tokens budget for model generation fallback.
    # Default 4096 allows complete responses without over-running typical Telegram payloads.
    # Model limits in ModelCatalog take precedence per model descriptor.
    nvidia_max_tokens: int = 4096

    # HTTP client timeout in seconds for NVIDIA API requests.
    # Default 90.0 seconds accommodates complex model reasoning generation.
    nvidia_timeout_seconds: float = 90.0

    # OpenRouter Provider
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: float = 90.0

    # Ollama Provider
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_enabled: bool = True
    ollama_timeout_seconds: float = 60.0
    ollama_embedding_model: str = "bge-m3:latest"

    # Tool Intent Planner
    # Default 30.0s inactivity watchdog, matching Harness startup semantics.
    # 30s accommodates OpenRouter free-tier queueing (8–25s TTFT) and NVIDIA cold-start
    # (5–30s TTFT) that the previous 15s threshold was prematurely killing.
    # This is a per-candidate INACTIVITY timeout, not a total planning deadline.
    tool_planner_timeout: float = 30.0

    # Harness Provider Startup Timeout
    # Default 30.0s per-candidate pre-response activity watchdog.
    harness_startup_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
