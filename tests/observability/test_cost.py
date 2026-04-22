"""Per-model cost lookup + compute_cost table tests.

Pricing values are anchored to the cached-2026-04 public rates — if
Anthropic changes them, this file updates to match.
"""
from __future__ import annotations

import pytest

from shotcut.observability.cost import (
    compute_cost,
    pricing_for,
)


def test_known_model_pricing() -> None:
    opus_4_7 = pricing_for("claude-opus-4-7")
    assert opus_4_7.input_per_mtok == 5.0
    assert opus_4_7.output_per_mtok == 25.0

    sonnet_4_6 = pricing_for("claude-sonnet-4-6")
    assert sonnet_4_6.input_per_mtok == 3.0
    assert sonnet_4_6.output_per_mtok == 15.0

    haiku_4_5 = pricing_for("claude-haiku-4-5")
    assert haiku_4_5.input_per_mtok == 1.0
    assert haiku_4_5.output_per_mtok == 5.0


def test_date_suffix_canonicalizes() -> None:
    """`claude-haiku-4-5-20251001` reads as `claude-haiku-4-5`."""
    p = pricing_for("claude-haiku-4-5-20251001")
    assert p.input_per_mtok == 1.0


def test_unknown_model_falls_back_to_opus_4_7() -> None:
    """Conservative fallback — Opus 4.7 is the most expensive Claude, so
    unknown models never under-report cost."""
    p = pricing_for("claude-mythos-preview-20990101")
    assert p.input_per_mtok == 5.0
    assert p.output_per_mtok == 25.0


def test_cached_rates_derive_from_input() -> None:
    p = pricing_for("claude-opus-4-7")
    assert p.cached_read_per_mtok == pytest.approx(0.5)  # 10% of $5
    assert p.cached_write_per_mtok == pytest.approx(6.25)  # 125% of $5


def test_compute_cost_basic_input_output() -> None:
    cost = compute_cost(
        "claude-opus-4-7", input_tokens=1_000_000, output_tokens=1_000_000
    )
    # 1M in @ $5 + 1M out @ $25 = $30.
    assert cost == pytest.approx(30.0)


def test_compute_cost_respects_cached_tokens() -> None:
    cost = compute_cost(
        "claude-opus-4-7",
        input_tokens=0,
        output_tokens=0,
        cached_read_tokens=1_000_000,  # 10% of $5 = $0.50
        cached_write_tokens=1_000_000,  # 125% of $5 = $6.25
    )
    assert cost == pytest.approx(6.75)


def test_compute_cost_zero_when_no_tokens() -> None:
    assert compute_cost("claude-haiku-4-5") == 0.0


def test_compute_cost_small_amounts() -> None:
    """Realistic single-request magnitudes — per-token accuracy matters."""
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=500,
    )
    # 2000 * $3/1M = $0.006; 500 * $15/1M = $0.0075; total $0.0135.
    assert cost == pytest.approx(0.0135)
