"""End-to-end CLI: `python -m evals.cli golden` passes against the
committed baseline + ships deterministic framework-only producer.

This also catches the acceptance criterion "baseline comparison is
deterministic: same commit, same input set, produces same verdict"
by running the CLI twice and asserting identical verdicts.
"""
from __future__ import annotations

import subprocess
import sys



def _run_cli(suite: str, *, gate: bool = True) -> subprocess.CompletedProcess:
    args = [sys.executable, "-m", "evals.cli", suite]
    if gate:
        args.append("--gate")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        env={
            "ANTHROPIC_API_KEY": "ci-dummy-key",
            "AUTH_DISABLED": "true",
            "PYTHONPATH": "src",
            "HOME": "/tmp",
        },
    )


def test_golden_suite_passes_gate_against_committed_baseline() -> None:
    result = _run_cli("golden", gate=True)
    assert result.returncode == 0, (
        f"golden gate should pass against committed baseline; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "golden: 3/3 passed" in result.stdout


def test_spreadsheetbench_empty_passes_gate() -> None:
    """No dataset → nothing runs → gate treats it as 'nothing ran' and
    lets the PR through. Prevents the bench gate from blocking PRs in
    environments that haven't wired the dataset yet."""
    result = _run_cli("spreadsheetbench", gate=True)
    assert result.returncode == 0, (
        f"empty bench run shouldn't fail the gate; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_golden_run_is_deterministic() -> None:
    """Same commit, same input → same verdicts. Verifies the acceptance
    criterion explicitly."""
    first = _run_cli("golden", gate=False)
    second = _run_cli("golden", gate=False)
    assert first.returncode == 0 and second.returncode == 0
    # Strip trace ids which include a fresh UUID per run.
    first_sans = "\n".join(
        line for line in first.stdout.splitlines() if "trace" not in line
    )
    second_sans = "\n".join(
        line for line in second.stdout.splitlines() if "trace" not in line
    )
    assert first_sans == second_sans


def test_missing_cassette_surfaces_as_ERRORED_prefix(
    monkeypatch, capsys
) -> None:
    """A golden case without a cassette produces a verdict=errored
    CaseResult and the CLI prefixes its stderr line with ERRORED, not
    FAILED — so a triager can distinguish schema/scaffolding breakage
    from a real workbook regression at a glance."""
    import evals.cli as cli_module
    from evals.recording import CassetteMissing

    def _always_missing(case_id: str):
        raise CassetteMissing(f"no cassette for {case_id!r}")

    monkeypatch.setattr(cli_module, "recorded_trace_producer", _always_missing)
    monkeypatch.setattr(sys, "argv", ["evals.cli", "golden", "--gate"])

    exit_code = cli_module.main()
    captured = capsys.readouterr()

    assert exit_code == 1, (
        f"gate should fail on missing cassettes; stdout={captured.out!r}"
    )
    # Per-case lines use ERRORED, not FAILED.
    assert "ERRORED sum_of_column" in captured.err
    assert "FAILED sum_of_column" not in captured.err
    # Summary line also shows the errored-bucket count.
    assert "golden errored (3)" in captured.out
