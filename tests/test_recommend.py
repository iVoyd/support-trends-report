import copy

from conftest import SEED_THEMES, FakeLLM

from trends import recommend, themes
from trends.trends import IssueRef, ThemeTrend

TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})
STORAGE = next(t for t in TS.themes if t.id == "storage")
URL = "https://github.com/supabase/supabase/issues/{}"
ISSUES = [IssueRef(n, f"Upload fails {n}", URL.format(n), 10 - n, 1, "open", ["storage"])
          for n in range(1, 6)]
TREND = ThemeTrend("storage", 12, 2, [1, 2, 2, 2])
ALLOWED = {i.url for i in ISSUES}


def act(citations, kind="docs_fix", title="Fix the upload docs", detail="Say the limit."):
    return {"kind": kind, "title": title, "detail": detail, "citations": citations}


def test_valid_actions_pass():
    actions, problems = recommend.validate_actions(
        {"actions": [act([URL.format(1), URL.format(2) + "/"])]}, ALLOWED)
    assert problems == [] and actions[0].citations == [URL.format(1), URL.format(2)]


def test_invented_and_missing_citations_are_rejected():
    actions, problems = recommend.validate_actions(
        {"actions": [act([URL.format(1), URL.format(999)]), act([])]}, ALLOWED)
    assert actions == []
    assert "issues/999" in problems[0] and "cites no issues" in problems[1]


def test_bad_kind_empty_text_and_too_many_actions():
    actions, problems = recommend.validate_actions(
        {"actions": [act([URL.format(1)], kind="blog_post"), act([URL.format(1)], title=""),
                     act([URL.format(1)])]}, ALLOWED)
    assert actions == []
    text = " ".join(problems)
    assert "kept the first 2" in text and "unknown kind" in text and "empty title" in text


def test_no_actions():
    assert recommend.validate_actions({"actions": []}, ALLOWED) == ([], ["no actions returned"])


def test_prompt_contains_only_given_issues():
    p = recommend.user_prompt(STORAGE, TREND, ISSUES[:2])
    assert URL.format(1) in p and URL.format(2) in p and URL.format(3) not in p
    assert "This week: 12 new issues" in p and "Average of the previous 4 weeks: 1.8" in p


def test_retry_fixes_bad_citation():
    fake = FakeLLM(bad_citations=1)
    rec, usage = recommend.recommend_theme(fake, STORAGE, TREND, ISSUES)
    assert len(fake.calls) == 2 and usage.calls == 2
    assert "issues/999999" in fake.calls[1]["user"]  # the retry is told what was wrong
    assert len(rec.actions) == 2
    assert all(c in ALLOWED for a in rec.actions for c in a.citations)
    assert any("not in the input" in p for p in rec.problems)


def test_persistent_bad_citation_drops_only_that_action():
    rec, _ = recommend.recommend_theme(FakeLLM(bad_citations=5), STORAGE, TREND, ISSUES)
    assert [a.kind for a in rec.actions] == ["faq_entry"]  # the clean one survives


def test_recommend_top_three_and_skips_other():
    rising = [ThemeTrend(t, 10, 1, [1, 1, 1, 1]) for t in
              ("other", "storage", "auth", "realtime", "docs")]
    issues = {t: ISSUES for t in TS.ids}
    fake = FakeLLM()
    recs, usage = recommend.recommend(fake, {t.id: t for t in TS.themes}, rising, issues)
    assert [r.theme for r in recs] == ["storage", "auth", "realtime"]
    assert usage.calls == 3
