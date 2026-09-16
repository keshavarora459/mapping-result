import asyncio
import json
import logging
import os
import random
from typing import Any, Dict, List, Optional

try:
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AsyncGroq,
        RateLimitError,
    )
except ImportError:
    class _MockChatCompletions:
        async def create(self, *args, **kwargs):
            raise RuntimeError("The 'groq' package is not installed. Please install it using 'pip install groq'.")

    class _MockChat:
        def __init__(self):
            self.completions = _MockChatCompletions()

    class AsyncGroq:
        def __init__(self, *args, **kwargs):
            self.chat = _MockChat()
    class APIConnectionError(Exception): pass
    class APIStatusError(Exception): pass
    class APITimeoutError(Exception): pass
    class RateLimitError(Exception): pass

from config import Config
from common.llm import redact_sensitive_data

logger = logging.getLogger(__name__)

# Caps concurrent Groq requests across every DashboardObjectConverter/GroqLLMClient
# instance in the process. Without this, a burst of dashboard-visual conversions
# (one Groq call per visual, see coordinator_agent.py) can fire dozens of
# simultaneous requests and blow through a low-quota key's rate limit almost
# immediately. Module-level so it's shared even though a new GroqLLMClient is
# constructed per ConversionContext.
_LLM_SEMAPHORE = asyncio.Semaphore(Config.LLM_MAX_CONCURRENCY)

# Errors worth retrying: rate limits, transient connectivity/timeouts, and
# 5xx responses. Anything else (bad request, auth, 4xx) fails fast.
_RETRYABLE_EXCEPTIONS = (RateLimitError, APIConnectionError, APITimeoutError)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status is not None and (status == 429 or status >= 500)
    return False


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Honor the API's Retry-After header when present, else None."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
    return None


class GroqLLMClient:
    """Async client for Groq API."""

    def __init__(self):
        logger.info("Initializing Groq LLM Client")
        self.api_key = Config.GROQ_API_KEY
        self.client = AsyncGroq(
            api_key=self.api_key or "unconfigured_key",
            timeout=float(os.getenv("LLM_TIMEOUT", "30.0")),
        )
        self.model = Config.GROQ_MODEL

    async def _complete(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]] = None,
        expect_json: bool = False,
    ) -> Any:
        """One chat completion, with retry/backoff and concurrency capping.
        """
        Config.validate()
        last_exc: Optional[Exception] = None

        for attempt in range(Config.MAX_RETRIES + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.0,
                }
                if response_format:
                    kwargs["response_format"] = response_format

                async with _LLM_SEMAPHORE:
                    response = await self.client.chat.completions.create(**kwargs)

                content = response.choices[0].message.content
                if not expect_json:
                    return content or ""

                try:
                    return json.loads(content)
                except json.JSONDecodeError as parse_exc:
                    # A truncated/malformed response is retried like a
                    # transient failure rather than raised straight away.
                    last_exc = parse_exc
                    logger.warning(
                        "Groq response was not valid JSON on attempt %d/%d: %s",
                        attempt + 1, Config.MAX_RETRIES + 1, redact_sensitive_data(parse_exc),
                    )
            except Exception as exc:  # noqa: BLE001 - reclassified below
                last_exc = exc
                redacted_exc = redact_sensitive_data(exc)
                if not _is_retryable(exc) or attempt == Config.MAX_RETRIES:
                    logger.error("Error calling Groq API: %s", redacted_exc)
                    raise
                logger.warning(
                    "Retryable Groq API error on attempt %d/%d: %s",
                    attempt + 1, Config.MAX_RETRIES + 1, redacted_exc,
                )

            if attempt == Config.MAX_RETRIES:
                break

            delay = _retry_after_seconds(last_exc)
            if delay is None:
                delay = Config.RETRY_DELAY * (2 ** attempt)
            delay += random.uniform(0, delay * 0.3)
            await asyncio.sleep(delay)

        logger.error("Groq API call failed after %d attempts: %s", Config.MAX_RETRIES + 1, last_exc)
        raise last_exc

    async def generate_structured_response(
        self, system_prompt: str, user_prompt: str, json_schema: dict
    ) -> Dict[str, Any]:
        """Calls Groq API to return a structured JSON object."""
        schema_str = json.dumps(json_schema, indent=2)
        full_system_prompt = (
            f"{system_prompt}\n\nYou must respond in JSON format matching this schema:\n{schema_str}"
        )
        return await self._complete(
            messages=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            expect_json=True,
        )

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        """Calls Groq API for a plain-text answer (a DAX expression).

        JSON mode is deliberately not used here: wrapping a DAX expression in
        a JSON string forces the model to escape every quote and bracket,
        which is exactly where malformed output comes from.
        """
        content = await self._complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            expect_json=False,
        )
        return (content or "").strip()
