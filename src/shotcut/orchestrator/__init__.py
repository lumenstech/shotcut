"""Stage 6 durable orchestration package.

Re-exports so callers can write `from shotcut.orchestrator import
durable, checkpoint, events` uniformly. The synchronous orchestrator
at `shotcut.agents.orchestrator` is still available for direct use
(tests, legacy paths); `durable` is the Stage 6 preferred entry point.
"""
from shotcut.orchestrator import checkpoint, durable, events

__all__ = ["checkpoint", "durable", "events"]
