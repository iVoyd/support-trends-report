import copy

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import github, themes


def theme_data(**overrides):
    data = {"version": 1, "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]}
    data.update(overrides)
    return data


def test_valid_list():
    ts = themes.validate(theme_data())
    assert ts.ids[-1] == "other" and len(ts.themes) == 9


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["themes"].append(dict(d["themes"][0])), "duplicate"),
    (lambda d: d["themes"].pop(), "other"),
    (lambda d: d["themes"][0].update(id="Auth Stuff"), "lowercase"),
    (lambda d: d["themes"][0].update(description=""), "description"),
    (lambda d: d["themes"][0].update(labels="auth"), "labels"),
    (lambda d: d.update(themes=d["themes"][:2] + [themes.OTHER_THEME]), "need"),
    (lambda d: d.update(version=0), "version"),
    (lambda d: d.update(themes="auth, storage"), "list"),
])
def test_invalid_lists(mutate, message):
    data = theme_data()
    mutate(data)
    with pytest.raises(themes.ThemeError, match=message):
        themes.validate(data)


def test_dump_load_roundtrip(tmp_path):
    ts = themes.validate(theme_data(repo=REPO, created="2026-11-03", model="m"))
    path = tmp_path / "themes.yaml"
    themes.dump(ts, path)
    assert path.read_text().startswith("# Frozen theme list")
    assert themes.load(path) == ts


def test_load_missing_file_explains(tmp_path):
    with pytest.raises(themes.ThemeError, match="seed-themes"):
        themes.load(tmp_path / "nope.yaml")


def test_seed_uses_labels_and_adds_other(conn):
    since = github.window_start(8, FIXED_NOW)
    github.fetch_issues(conn, REPO, since, http_get=FakeGitHub(), per_page=50,
                        sleep=lambda _: None)
    fake = FakeLLM()
    ts, usage = themes.seed(fake, conn, REPO, since, sample_size=30)

    prompt = fake.calls[0]["user"]
    assert "- storage:" in prompt and "- auth:" in prompt  # label counts are in the prompt
    assert prompt.count("\n- #") == 30
    assert ts.ids[-1] == "other" and ts.ids.count("other") == 1
    auth = next(t for t in ts.themes if t.id == "auth")
    assert auth.labels == ("auth",)  # invented label dropped
    assert ts.model == "claude-haiku-5-5" and usage.calls == 1


def test_seed_needs_issues(conn):
    with pytest.raises(themes.ThemeError, match="fetch"):
        themes.seed(FakeLLM(), conn, REPO, "2026-01-01T00:00:00Z")


def test_sample_is_deterministic():
    rows = [{"number": i} for i in range(100)]
    assert themes.sample_issues(rows, 10) == themes.sample_issues(rows, 10)
    assert len(themes.sample_issues(rows, 200)) == 100
