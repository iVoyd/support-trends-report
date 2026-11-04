"""End to end through the CLI with fake GitHub and fake LLM: fetch -> seed -> theme -> status."""

import sqlite3

import pytest
from conftest import FIXED_NOW, FakeGitHub, FakeLLM

from trends import cli, github, llm

real_window_start = github.window_start
CLIENT_SETTINGS: list[dict] = []  # keyword settings each AnthropicJSON was created with


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github, "window_start", lambda weeks: real_window_start(weeks, FIXED_NOW))
    monkeypatch.setattr(github, "urllib_get", FakeGitHub())
    CLIENT_SETTINGS.clear()

    def fake_client(model, **settings):
        CLIENT_SETTINGS.append(settings)
        return FakeLLM()

    monkeypatch.setattr(llm, "AnthropicJSON", fake_client)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(["--db", "t.db", *argv])
    return code, capsys.readouterr().out


def test_full_milestone_one_flow(env, capsys):
    code, out = run(capsys, "fetch", "--weeks", "8")
    assert code == 0 and "issues stored" in out

    code, out = run(capsys, "theme")
    assert code == 1  # no themes.yaml yet

    code, out = run(capsys, "seed-themes")
    assert code == 0 and (env / "themes.yaml").exists()
    code, out = run(capsys, "seed-themes")
    assert code == 1 and "frozen" in out  # refuses to overwrite

    code, out = run(capsys, "theme", "--estimate")
    assert code == 0 and "rough estimate" in out

    code, out = run(capsys, "theme")
    assert code == 0 and "failed: 0" in out and "cost: $" in out

    code, out = run(capsys, "theme")
    assert "Nothing to theme" in out

    code, out = run(capsys, "status")
    lines = [ln.split() for ln in out.splitlines() if ln.startswith("2026-W")]
    assert len(lines) == 8  # 8 ISO weeks, W38..W45
    assert all(issues == themed for _, issues, themed in lines)  # every week fully themed
    assert "LLM theme: 1 runs" in out

    db = sqlite3.connect(env / "t.db")
    runs = db.execute("SELECT command, calls, cost_usd FROM llm_runs ORDER BY id").fetchall()
    assert [r[0] for r in runs] == ["seed-themes", "theme"]
    assert all(r[1] > 0 and r[2] > 0 for r in runs)


def test_reseed_with_force_bumps_version(env, capsys):
    run(capsys, "fetch")
    run(capsys, "seed-themes")
    code, _ = run(capsys, "seed-themes", "--force")
    assert code == 0 and "version: 2" in (env / "themes.yaml").read_text()


def themed_db(capsys):
    for cmd in (["fetch"], ["seed-themes"], ["theme"]):
        assert run(capsys, *cmd)[0] == 0


def test_report_writes_week_file(env, capsys):
    themed_db(capsys)
    code, out = run(capsys, "report", "--week", "2026-W44")
    assert code == 0 and "1 rising themes" in out
    md = (env / "reports" / "2026-W44.md").read_text()
    assert md.startswith("# What's breaking: supabase/supabase, 2026-W44")
    assert "- **Storage**: " in md and "### Storage" in md and "Based on: [#" in md

    # every cited issue URL in the report is an issue in the database
    import re
    db = sqlite3.connect(env / "t.db")
    known = {r[0] for r in db.execute("SELECT html_url FROM issues")}
    cited = set(re.findall(r"\((https://github\.com/[^)]+/issues/\d+)\)", md))
    assert cited and cited <= known
    assert db.execute("SELECT COUNT(*) FROM llm_runs WHERE command = 'report'").fetchone()[0]


def test_report_without_llm_and_bad_week(env, capsys):
    themed_db(capsys)
    code, _ = run(capsys, "report", "--week", "2026-W44", "--no-llm")
    assert code == 0
    assert "Skipped (`--no-llm`)" in (env / "reports" / "2026-W44.md").read_text()
    assert run(capsys, "report", "--week", "2020-W01", "--no-llm")[0] == 1
    assert run(capsys, "report", "--week", "W44")[0] == 1


def test_report_reuses_saved_recommendations(env, capsys):
    themed_db(capsys)
    run(capsys, "report", "--week", "2026-W44")
    first = (env / "reports" / "2026-W44.md").read_text()
    code, out = run(capsys, "report", "--week", "2026-W44")
    assert code == 0 and "reused from an earlier run" in out
    assert (env / "reports" / "2026-W44.html").exists()
    db = sqlite3.connect(env / "t.db")
    report_runs = lambda: db.execute(  # noqa: E731
        "SELECT COUNT(*) FROM llm_runs WHERE command = 'report'").fetchone()[0]
    assert report_runs() == 1
    second = (env / "reports" / "2026-W44.md").read_text()
    assert first.split("## All themes")[0].split("## Recommendations")[1] == \
        second.split("## All themes")[0].split("## Recommendations")[1]
    run(capsys, "report", "--week", "2026-W44", "--regenerate")
    assert report_runs() == 2


def test_check_report_command(env, capsys):
    themed_db(capsys)
    run(capsys, "report", "--week", "2026-W44", "--no-llm")
    code, out = run(capsys, "check-report", "reports/2026-W44.md")
    assert code == 0 and "matches the database" in out
    path = env / "reports" / "2026-W44.md"
    path.write_text(path.read_text().replace("| Storage | 12 |", "| Storage | 13 |"))
    code, out = run(capsys, "check-report", "reports/2026-W44.md")
    assert code == 3 and "Storage" in out
    code, out = run(capsys, "check-report", "reports/2026-W44.md", "--internal-only")
    assert code == 3


def test_theme_and_status_cover_every_stored_week(env, capsys):
    """Regression: fetch --weeks 10 then theme (default) used to theme only 8 weeks."""
    run(capsys, "fetch", "--weeks", "10")
    run(capsys, "seed-themes")
    code, out = run(capsys, "theme")
    assert code == 0 and "failed: 0" in out
    code, out = run(capsys, "status")
    lines = [ln.split() for ln in out.splitlines() if ln.startswith("2026-W")]
    assert [ln[0] for ln in lines][:2] == ["2026-W36", "2026-W37"] and len(lines) == 10
    assert all(issues == themed for _, issues, themed in lines)


def test_report_warns_about_unthemed_weeks_then_clears(env, capsys):
    run(capsys, "fetch", "--weeks", "10")
    run(capsys, "seed-themes")
    run(capsys, "theme", "--weeks", "8")      # leaves W36 and W37 unthemed
    code, out = run(capsys, "report", "--week", "2026-W43")
    assert code == 0
    assert "warning: issues without a theme in 2026-W36" in out
    html = (env / "reports" / "2026-W43.html").read_text()
    assert "Some weeks have issues without a theme" in html and "2026-W37 (" in html
    md = (env / "reports" / "2026-W43.md").read_text()
    assert "without a theme in earlier weeks" not in md  # W39-W42 history is fully themed

    run(capsys, "theme")
    code, out = run(capsys, "report", "--week", "2026-W43")
    assert "warning" not in out
    assert "without a theme" not in (env / "reports" / "2026-W43.html").read_text()


def test_markdown_warns_when_history_is_unthemed(env, capsys):
    run(capsys, "fetch", "--weeks", "10")
    run(capsys, "seed-themes")
    run(capsys, "theme", "--weeks", "3")      # W43-W45 only; W39-W42 history unthemed
    run(capsys, "report", "--week", "2026-W43", "--no-llm")
    md = (env / "reports" / "2026-W43.md").read_text()
    assert "Issues without a theme in earlier weeks: 2026-W39 (" in md


def test_compare_themes_command(env, capsys):
    themed_db(capsys)
    text = (env / "themes.yaml").read_text().replace("version: 1", "version: 2")
    (env / "themes.yaml").write_text(text)
    code, out = run(capsys, "compare-themes")
    assert code == 0 and "not yet v2" in out
    run(capsys, "theme")
    code, out = run(capsys, "compare-themes", "--week", "2026-W44")
    assert code == 0 and "v1 vs v2, 2026-W44" in out and "| **Total** | 24 | 24 |" in out


def test_stability_command(env, capsys):
    themed_db(capsys)
    code, out = run(capsys, "stability", "--sample", "30", "--shuffle", "--effort", "medium")
    assert code == 0
    assert "Same theme in every pass: 30 of 30 (100.0%)" in out
    assert "shuffled between passes" in out and "Nothing was saved" in out
    assert CLIENT_SETTINGS[-1] == {"effort": "medium", "thinking": "disabled"}
    db = sqlite3.connect(env / "t.db")
    assert db.execute("SELECT COUNT(*) FROM llm_runs WHERE command = 'stability'"
                      ).fetchone()[0] == 1


def test_theme_passes_model_settings(env, capsys):
    run(capsys, "fetch")
    run(capsys, "seed-themes")
    run(capsys, "theme", "--thinking", "adaptive", "--effort", "high")
    assert CLIENT_SETTINGS[-1] == {"effort": "high", "thinking": "adaptive"}
