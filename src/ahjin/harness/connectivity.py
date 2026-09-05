"""ConnectivityChecker — Clean cloud environment availability detection.

Determines whether AHJIN has access to the global cloud environment.
- Respects explicit offline_mode settings.
- Checks if cloud provider API keys and registered cloud models are available.
"""

import structlog

from ahjin.core.config import settings

logger = structlog.get_logger()


class ConnectivityChecker:
    """Determines whether the global cloud environment is reachable/usable."""

    def __init__(self, force_offline: bool = False) -> None:
        self._force_offline = force_offline

    @property
    def is_force_offline(self) -> bool:
        return self._force_offline

    def set_force_offline(self, force_offline: bool) -> None:
        self._force_offline = force_offline

    def is_online(self) -> bool:
        """Return True if AHJIN can attempt global cloud model access.

        Returns False if:
        1. force_offline is set to True (explicit offline mode).
        2. settings.offline_mode is set to True.
        3. No cloud API keys (NVIDIA / OpenRouter) are configured.
        """
        if self._force_offline or getattr(settings, "offline_mode", False):
            logger.info("Offline mode active — cloud environment marked unavailable")
            return False

        # Check if at least one cloud provider API key is present
        has_nvidia = bool(settings.nvidia_api_key)
        has_openrouter = bool(settings.openrouter_api_key)

        if not (has_nvidia or has_openrouter):
            logger.info("No cloud API keys configured — running in offline mode")
            return False

        return True
