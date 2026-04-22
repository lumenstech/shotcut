"""Anthropic SDK wrapper.

One shared `AsyncAnthropic` client per process. Agents call this module
rather than constructing their own clients so we can layer retries,
telemetry, and test fakes in one place.

Defaults follow the claude-api skill guidance:
  - model: claude-opus-4-7
  - thinking: adaptive
  - effort: high
  - streaming for large max_tokens
"""
from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic

from shotcut.config import settings


@lru_cache(maxsize=1)
def get_client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def cached_system(text: str) -> list[dict]:
    """Render a system prompt as a single cacheable block.

    The minimum cacheable prefix on Opus 4.7 is 4096 tokens — short prompts
    won't actually cache, but the marker is harmless.
    """
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
