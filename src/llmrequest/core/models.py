# src/llmrequest/core/models.py

import random
import time
from typing import TypedDict

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


# --- TypedDict for Generation Parameters ---
class GenerationParams(TypedDict, total=False):
    """A TypedDict for specifying generation parameters for an LLM request.

    Attributes:
    ----------
    temperature : float
        Sampling temperature for randomness in generation.
    maxOutputTokens : int
        Maximum number of tokens in the output.
    topP : float
        Nucleus sampling parameter.
    topK : int
        Top-k sampling parameter.
    stopSequences : list[str]
        List of sequences where generation should stop.
    candidateCount : int
        Number of candidate responses to generate.
    presence_penalty : float
        Penalty for token presence in the output.
    frequency_penalty : float
        Penalty for token frequency in the output.
    seed : int
        Random seed for reproducibility.
    user : str
        Identifier for the user making the request.
    response_mime_type : str
        MIME type of the response.
    responseSchema : dict
        Schema for validating the response.
    encoding_format : str
        Encoding format for the input/output.
    dimensions : int
        Dimensionality of the input/output.
    """

    temperature: float
    maxOutputTokens: int
    topP: float
    topK: int
    stopSequences: list[str]
    candidateCount: int
    presence_penalty: float
    frequency_penalty: float
    seed: int
    user: str
    response_mime_type: str
    responseSchema: dict
    encoding_format: str
    dimensions: int


# --- Custom Exceptions ---
class LLMClientError(Exception):
    """Base exception for LLM client errors."""

    pass


class APIKeyError(LLMClientError):
    """For errors related to invalid or missing API keys."""

    pass


class NetworkError(LLMClientError):
    """For network-related errors like connection issues or timeouts."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        original_exception: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.original_exception = original_exception


class RateLimitError(NetworkError):
    """For 429 rate limit errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_text: str | None = None,
        key_identifier: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.response_text = response_text
        self.key_identifier = key_identifier


class PermissionDeniedError(NetworkError):
    """For 401/403 permission errors."""

    def __init__(
        self,
        message: str,
        status_code: int,
        response_text: str | None = None,
        key_identifier: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.response_text = response_text
        self.key_identifier = key_identifier


class APIResponseError(LLMClientError):
    """For general non-2xx API responses."""

    def __init__(
        self, message: str, status_code: int | None = None, response_text: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class PayloadTooLargeError(NetworkError):
    """For 413 payload too large errors."""

    def __init__(
        self, message: str, status_code: int = 413, response_text: str | None = None
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.response_text = response_text


# --- API Key Manager ---
class APIKeyManager:
    """A dedicated class to manage the lifecycle of API keys."""

    def __init__(
        self,
        initial_keys: list[str],
        abandoned_keys_config: set[str],
        rate_limit_disable_seconds: int,
    ) -> None:
        self.initial_keys = initial_keys
        self.abandoned_keys_config = abandoned_keys_config
        self.rate_limit_disable_duration_seconds = rate_limit_disable_seconds
        self._abandoned_keys_runtime: set[str] = set()
        self._temporarily_disabled_keys: dict[str, float] = {}

    def get_available_keys(self) -> list[str]:
        """Returns a shuffled list of currently available keys."""
        current_time = time.time()

        # Reactivate keys whose disable duration has passed
        keys_to_reactivate = [
            k
            for k, expiry_ts in self._temporarily_disabled_keys.items()
            if expiry_ts <= current_time
        ]
        for k in keys_to_reactivate:
            del self._temporarily_disabled_keys[k]
            logger.info(f"密钥 ...{k[-4:]} 的429临时禁用已到期并解除。")

        all_abandoned = self.abandoned_keys_config.union(self._abandoned_keys_runtime)

        available = [
            key
            for key in self.initial_keys
            if key not in all_abandoned and key not in self._temporarily_disabled_keys
        ]

        random.shuffle(available)
        return available

    def temporarily_disable_key(self, key: str) -> None:
        """Temporarily disables a key due to rate limiting."""
        if self.rate_limit_disable_duration_seconds > 0:
            disable_until = time.time() + self.rate_limit_disable_duration_seconds
            self._temporarily_disabled_keys[key] = disable_until
            ban_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(disable_until))
            logger.info(f"密钥 ...{key[-4:]} 已被临时禁用直到 {ban_time_str}.")

    def permanently_abandon_key(self, key: str) -> None:
        """Permanently abandons a key for the current runtime."""
        self._abandoned_keys_runtime.add(key)
        # Also remove it from temporary disable list if it's there
        self._temporarily_disabled_keys.pop(key, None)

    def reset_temporary_disable_list(self) -> None:
        """Clears the list of temporarily disabled keys."""
        logger.warning("正在清除所有临时禁用的API密钥列表以进行重试...")
        self._temporarily_disabled_keys.clear()
