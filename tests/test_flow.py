"""The Prefect flow: steps run in order, fetch is retried, a failed report check fails the run."""

import pytest

pytest.importorskip("prefect")

from prefect.testing.utilities import prefect_test_harness

from trends import flow


@pytest.fixture(scope="module", autouse=True)
def harness():
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(flow.fetch, "retry_delay_seconds", 0)
    monkeypatch.setattr(flow.theme, "retry_delay_seconds", 0)


def fake_cli(monkeypatch, codes):
    """Replace cli.main; `codes` maps a step to the exit codes it returns, in order."""
    calls = []

    def main(argv):
        step = argv[2]
        calls.append(step)
        seq = codes.get(step, [0])
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(flow.cli, "main", main)
    return calls


def test_steps_run_in_order(monkeypatch, tmp_path):
    calls = fake_cli(monkeypatch, {})
    flow.weekly_report(db=str(tmp_path / "t.db"), week="2026-W45")
    assert calls == ["fetch", "theme", "report"]


def test_fetch_is_retried(monkeypatch, tmp_path):
    calls = fake_cli(monkeypatch, {"fetch": [1, 0]})
    flow.weekly_report(db=str(tmp_path / "t.db"))
    assert calls == ["fetch", "fetch", "theme", "report"]


def test_failed_report_check_fails_the_run(monkeypatch, tmp_path):
    calls = fake_cli(monkeypatch, {"report": [3]})
    with pytest.raises(flow.StepFailed, match="report exited with code 3"):
        flow.weekly_report(db=str(tmp_path / "t.db"))
    assert calls == ["fetch", "theme", "report"]
