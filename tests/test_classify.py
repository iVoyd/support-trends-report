import copy

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, github, themes
from trends.llm import LLMFatalError

SINCE = github.window_start(8, FIXED_NOW)
TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})
IDS = set(TS.ids)


@pytest.fixture
def loaded(conn):
    github.fetch_issues(conn, REPO, SINCE, http_get=FakeGitHub(), per_page=50,
                        sleep=lambda _: None)
    return conn


def a(number, theme="auth", confidence="high"):
    return {"number": number, "theme": theme, "confidence": confidence}


def test_validate_accepts_complete_answer():
    valid, problems = classify.validate_assignments(
        {"assignments": [a(1), a(2, "storage", "low")]}, {1, 2}, IDS)
    assert valid == {1: ("auth", "high"), 2: ("storage", "low")} and problems == []


def test_validate_rejects_bad_entries():
    data = {"assignments": [
        a(1, "made-up"),          # unknown theme
        a(2, confidence="sure"),  # bad confidence
        a(3), a(3, "storage"),    # contradictory duplicate
        a(4), a(4),               # harmless duplicate
        a(99),                    # not in batch
        "junk",
    ]}
    valid, problems = classify.validate_assignments(data, {1, 2, 3, 4, 5}, IDS)
    assert valid == {4: ("auth", "high")}
    text = "\n".join(problems)
    for expected in ("#1: unknown theme", "#2: bad confidence", "#3: returned 2 times",
                     "#99: not in this batch", "#5: missing", "non-object"):
        assert expected in text


def test_validate_rejects_wrong_shape():
    assert classify.validate_assignments({"oops": []}, {1}, IDS)[0] == {}
    assert classify.validate_assignments([], {1}, IDS)[0] == {}


def test_schema_limits_themes_to_list():
    item = classify.schema(TS)["properties"]["assignments"]["items"]
    assert item["properties"]["theme"]["enum"] == TS.ids
    assert item["additionalProperties"] is False


def test_system_prompt_is_stable_and_lists_every_theme():
    p = classify.system_prompt(TS)
    assert p == classify.system_prompt(TS)  # byte-identical, so the cache can hit
    for t in TS.themes:
        assert f"- {t.id}: " in p


def test_theme_all_issues(loaded):
    fake = FakeLLM()
    res = classify.theme_issues(fake, loaded, TS, REPO, SINCE, batch_size=20)
    total = loaded.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    assert res.themed == total and res.failed == [] and res.problems == []
    assert len(fake.calls) == -(-total // 20)
    assert len({c["system"] for c in fake.calls}) == 1  # same cached prefix every call
    counts = dict(loaded.execute(
        "SELECT theme, COUNT(*) FROM issue_themes GROUP BY theme").fetchall())
    assert set(counts) <= IDS and counts["storage"] > counts["docs"]
    assert res.usage.calls == len(fake.calls)


def test_missing_and_invalid_answers_are_retried(loaded):
    first = [r["number"] for r in classify.unthemed(loaded, REPO, SINCE, 1)][:3]
    fake = FakeLLM(drop=[first[0]], bad_theme=[first[1]])
    res = classify.theme_issues(fake, loaded, TS, REPO, SINCE, batch_size=20)
    assert res.failed == []
    assert any("missing" in p for p in res.problems)
    assert any("unknown theme" in p for p in res.problems)
    retried = [c for c in fake.calls if c["user"].startswith("1 issues")]
    assert len(retried) == 2


def test_issue_that_never_validates_is_left_unthemed(loaded):
    stubborn = classify.unthemed(loaded, REPO, SINCE, 1)[0]["number"]
    res = classify.theme_issues(FakeLLM(always_drop=[stubborn]), loaded, TS, REPO, SINCE)
    assert res.failed == [stubborn]
    assert loaded.execute("SELECT 1 FROM issue_themes WHERE number = ?",
                          (stubborn,)).fetchone() is None


def test_rerun_only_does_new_work(loaded):
    classify.theme_issues(FakeLLM(), loaded, TS, REPO, SINCE, limit=10)
    fake = FakeLLM()
    res = classify.theme_issues(fake, loaded, TS, REPO, SINCE)
    assert res.skipped_already_done == 10
    assert sum(c["user"].count("### #") for c in fake.calls) == res.themed


def test_new_version_rethemes(loaded):
    classify.theme_issues(FakeLLM(), loaded, TS, REPO, SINCE)
    v2 = themes.ThemeSet(version=2, themes=TS.themes)
    assert len(classify.unthemed(loaded, REPO, SINCE, 2)) == loaded.execute(
        "SELECT COUNT(*) FROM issues").fetchone()[0]
    classify.theme_issues(FakeLLM(), loaded, v2, REPO, SINCE)
    assert loaded.execute("SELECT COUNT(DISTINCT themes_version) FROM issue_themes"
                          ).fetchone()[0] == 2


def test_fatal_error_stops_the_run(loaded):
    class Broken(FakeLLM):
        def call(self, *a, **k):
            raise LLMFatalError("AuthenticationError")

    with pytest.raises(LLMFatalError):
        classify.theme_issues(Broken(), loaded, TS, REPO, SINCE)
