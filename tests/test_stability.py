import copy

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, github, stability, themes

SINCE = github.window_start(8, FIXED_NOW)
TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})


class FlakyLLM(FakeLLM):
    """FakeLLM, but on calls after `flip_after` it answers 'other' for the given issues."""

    def __init__(self, flip=(), flip_after=0, **kw):
        super().__init__(**kw)
        self.flip, self.flip_after = set(flip), flip_after

    def call(self, system, user, schema, max_tokens=4096):
        data, usage = super().call(system, user, schema, max_tokens)
        if len(self.calls) > self.flip_after:
            for a in data.get("assignments", []):
                if a["number"] in self.flip:
                    a["theme"] = "other" if a["theme"] != "other" else "docs"
        return data, usage


@pytest.fixture
def loaded(conn):
    github.fetch_issues(conn, REPO, SINCE, http_get=FakeGitHub(), now=FIXED_NOW,
                        sleep=lambda _: None)
    return conn


def numbers(conn, n=None):
    return [r["number"] for r in stability.sample_rows(conn, REPO, n)]


def test_deterministic_model_agrees_fully_and_saves_nothing(loaded):
    res = stability.measure(FakeLLM(), loaded, TS, REPO)
    assert res.compared == res.issues == len(numbers(loaded)) and res.rate == 1.0
    assert res.disagreements == [] and res.failed == 0
    assert loaded.execute("SELECT COUNT(*) FROM issue_themes").fetchone()[0] == 0


def test_flipped_issues_are_counted(loaded):
    nums = numbers(loaded)
    calls_per_pass = -(-len(nums) // 20)
    flip = nums[:3]
    res = stability.measure(FlakyLLM(flip=flip, flip_after=calls_per_pass), loaded, TS, REPO)
    assert res.agreed == res.compared - 3
    assert sorted(n for n, _, _ in res.disagreements) == sorted(flip)
    assert sum(res.pairs.values()) == 3
    assert f"{res.agreed} of {res.compared}" in stability.render(res, {}, "test")


def test_sample_and_passes(loaded):
    fake = FakeLLM()
    res = stability.measure(fake, loaded, TS, REPO, sample=30, passes=3, batch_size=10)
    assert res.issues == 30 and res.passes == 3 and len(fake.calls) == 9
    assert numbers(loaded, 30) == numbers(loaded, 30)  # same sample every time
    with pytest.raises(ValueError):
        stability.measure(fake, loaded, TS, REPO, passes=1)


def test_shuffle_changes_batches_but_not_answers(loaded):
    fake = FakeLLM()
    res = stability.measure(fake, loaded, TS, REPO, sample=40, shuffle=True)
    half = len(fake.calls) // 2
    first = [c["user"] for c in fake.calls[:half]]
    second = [c["user"] for c in fake.calls[half:]]
    assert first != second and res.rate == 1.0


def test_compares_with_stored_themes(loaded):
    classify.theme_issues(FakeLLM(), loaded, TS, REPO, SINCE)
    nums = numbers(loaded)
    res = stability.measure(FlakyLLM(flip=nums[:2], flip_after=0), loaded, TS, REPO)
    assert res.stored_compared == len(nums)
    assert res.agreed_with_stored == len(nums) - 2  # pass 1 already flipped those two
    assert res.rate == 1.0  # both passes flipped them the same way
