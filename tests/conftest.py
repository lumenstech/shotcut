"""Project-root pytest config.

Sets test-time environment variables before any test module imports
`shotcut.config` (which requires ANTHROPIC_API_KEY at construction time).
Actual Anthropic calls in tests are either stubbed or marked integration
and gated on a real key.
"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
