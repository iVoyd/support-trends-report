import json
import urllib.parse
from datetime import timedelta

import pytest
from conftest import FIXED_NOW, REPO, FakeGitHub, fixture_items

from trends import github


def raw_items():
    return fixture_items()


def no_sleep(_):
    pass


def test_window_start_is_monday_n_weeks_back():
    assert github.window_start(8, FIXED_NOW) == "2026-09-14T00:00:00Z"  # Monday of W38
    assert github.window_start(1, FIXED_NOW) == "2026-11-02T00:00:00Z"  # this Monday


def test_parse_issue_fields():
    raw = next(i for i in raw_items() if "pull_request" not in i and i["labels"])
    row = github.parse_issue(REPO, raw, "2026-11-03T00:00:00Z")
    assert row["number"] == raw["number"]
    assert row["labels"] == sorted(lbl["name"] for lbl in raw["labels"])
    assert row["reactions"] == raw["reactions"]["total_count"]
    assert row["thumbs_up"] == raw["reactions"]["+1"]
    assert row["comments"] == raw["comments"]
    assert row["state"] == raw["state"]
    assert row["html_url"] == f"https://github.com/{REPO}/issues/{raw['number']}"


def test_parse_issue_skips_pull_requests():
    pr = next(i for i in raw_items() if "pull_request" in i)
    assert github.parse_issue(REPO, pr, "x") is None


def test_parse_issue_tolerates_missing_and_odd_fields():
    raw = {"number": 7, "title": " Hi ", "created_at": "2026-10-01T00:00:00Z",
           "labels": ["plain-string", {"name": "obj"}, {"color": "no-name"}],
           "body": None, "reactions": None, "user": None, "comments": None}
    row = github.parse_issue(REPO, raw, "x")
    assert row["title"] == "Hi"
    assert row["labels"] == ["obj", "plain-string"]
    assert row["body"] == "" and row["reactions"] == 0 and row["comments"] == 0
    assert row["author"] is None
    assert row["updated_at"] == raw["created_at"]
    assert row["html_url"].endswith("/issues/7")


def test_parse_issue_truncates_long_body():
    raw = {"number": 1, "title": "t", "created_at": "2026-10-01T00:00:00Z", "body": "x" * 10000}
    assert len(github.parse_issue(REPO, raw, "x")["body"]) == github.BODY_LIMIT


def expected_issues(since):
    return [i for i in raw_items() if i["created_at"] >= since and "pull_request" not in i]


def fetch(conn, fake, **kw):
    kw.setdefault("now", FIXED_NOW)
    kw.setdefault("sleep", no_sleep)
    return github.fetch_issues(conn, REPO, github.window_start(8, FIXED_NOW), http_get=fake,
                               **kw)


def test_week_chunks_cover_window_to_now():
    chunks = github.week_chunks("2026-09-14T00:00:00Z", FIXED_NOW)
    assert len(chunks) == 8
    assert chunks[0][0].isoformat() == "2026-09-14T00:00:00+00:00"
    assert chunks[-1][1].isoformat() == "2026-11-09T00:00:00+00:00"
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:], strict=False))


def test_search_url_query():
    from datetime import datetime
    start = datetime(2026, 10, 26, tzinfo=FIXED_NOW.tzinfo)
    url = github.search_url(REPO, start, start + timedelta(weeks=1), 2, 100)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["q"] == ("repo:supabase/supabase is:issue "
                      "created:2026-10-26T00:00:00Z..2026-11-01T23:59:59Z")
    assert (q["sort"], q["order"], q["page"], q["per_page"]) == ("created", "asc", "2", "100")


def test_fetch_stores_all_eight_weeks(conn):
    since = github.window_start(8, FIXED_NOW)
    fake = FakeGitHub()
    res = fetch(conn, fake)
    stored = {r["number"] for r in conn.execute("SELECT number FROM issues")}
    assert stored == {i["number"] for i in expected_issues(since)}
    assert res.issues == len(stored) and res.weeks_read == 8 and res.splits == 0
    assert all("is:issue" in q for q in fake.queries)
    weeks = {r[0][:10] for r in conn.execute("SELECT created_at FROM issues")}
    assert min(weeks) >= "2026-09-14"
    # past weeks are done; the current week (W45) stays open so it is re-read next time
    done = [r["done"] for r in conn.execute("SELECT done FROM fetch_chunks ORDER BY start_at")]
    assert done == [1] * 7 + [0]


def test_fetch_pages_through_a_week(conn):
    fake = FakeGitHub()
    res = fetch(conn, fake, per_page=5)
    since = github.window_start(8, FIXED_NOW)
    assert res.issues == len(expected_issues(since))
    assert any("page=3" in u for u in fake.urls)


def test_never_requests_past_the_search_cap_and_splits_busy_weeks(conn):
    """The 422 case: GitHub refuses pages past its result cap. Busy weeks get split."""
    fake = FakeGitHub(cap=10)  # pretend the cap is 10 results; W44 has 24 issues
    res = fetch(conn, fake, per_page=5, cap=10)
    since = github.window_start(8, FIXED_NOW)
    assert res.splits > 0
    assert res.issues == len(expected_issues(since))
    assert {r["number"] for r in conn.execute("SELECT number FROM issues")} == {
        i["number"] for i in expected_issues(since)}


def test_422_raises_clear_error_and_rerun_resumes(conn):
    fake = FakeGitHub(fail_on={3: 422})  # third request: week 3 (W40)
    with pytest.raises(github.GitHubError, match="422"):
        fetch(conn, fake)
    assert len(fake.urls) == 3  # not retried
    assert conn.execute("SELECT COUNT(*) FROM fetch_chunks WHERE done = 1").fetchone()[0] == 2

    res = fetch(conn, fake)
    assert res.weeks_skipped == 2 and res.weeks_read == 6
    since = github.window_start(8, FIXED_NOW)
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == len(
        expected_issues(since))


def test_rerun_skips_done_weeks_unless_refresh(conn):
    fetch(conn, FakeGitHub())
    res = fetch(conn, FakeGitHub())
    assert (res.weeks_skipped, res.weeks_read) == (7, 1)  # only the current week again
    res = fetch(conn, FakeGitHub(), refresh=True)
    assert (res.weeks_skipped, res.weeks_read) == (0, 8)


def test_fetch_retries_server_errors(conn):
    slept = []
    fetch(conn, FakeGitHub(fail_on={1: 502}), sleep=slept.append)
    assert slept == [2]
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] > 0


def test_rate_limit_waits_when_reset_is_near():
    calls, slept = [], []

    def limited_once(url, headers):
        calls.append(url)
        if len(calls) == 1:
            return 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}, b"{}"
        return 200, {}, b"ok"

    body = github._get_with_retry(limited_once, "u", {}, slept.append, clock=lambda: 1000)
    assert body == b"ok" and slept == [31]


def test_rate_limit_far_off_gives_clear_error():
    def limited(url, headers):
        return 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "99999"}, b"{}"

    with pytest.raises(github.GitHubError, match="rate limit"):
        github._get_with_retry(limited, "u", {}, no_sleep, clock=lambda: 1000)


def test_secondary_rate_limit_honours_retry_after():
    slept, calls = [], []

    def secondary(url, headers):
        calls.append(1)
        return (403, {"Retry-After": "7"}, b"{}") if len(calls) == 1 else (200, {}, b"ok")

    assert github._get_with_retry(secondary, "u", {}, slept.append, clock=lambda: 0) == b"ok"
    assert slept == [8]


def test_missing_reactions_field_is_warned(conn):
    res = fetch(conn, FakeGitHub(drop_fields=("reactions",)))
    assert any("reactions" in w for w in res.warnings)


def test_fetch_records_raw_pages(conn, tmp_path):
    fetch(conn, FakeGitHub(), record_dir=tmp_path)
    files = sorted(tmp_path.glob("search_*_p1.json"))
    assert len(files) == 8
    assert "items" in json.loads(files[0].read_text())


def test_token_header_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    assert github.default_headers()["Authorization"] == "Bearer abc"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert "Authorization" not in github.default_headers()
