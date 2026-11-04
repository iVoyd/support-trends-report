"""Fetch issues from the GitHub search API into SQLite. Read-only: only GET requests.

Why search and not /repos/{repo}/issues: that endpoint lists pull requests too and, on big
repositories, refuses page numbers past about 10 (HTTP 422 "Pagination with the page
parameter is not supported for large datasets"). On supabase/supabase eight weeks of
issues + PRs is far more than 10 pages.

So the window is cut into ISO weeks and each week is one search query:
    repo:OWNER/NAME is:issue created:START..END
sorted by creation date. Search returns at most 1,000 results per query; a week with more
than that is split in half (and again, if needed) until each piece fits. A week is marked
done once all its pages are stored and the week is over, so an interrupted fetch resumes
at the first week not done. The current week is always re-read. `refresh=True` re-reads
every week (to update state, comments and reactions).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db

API = "https://api.github.com"
BODY_LIMIT = 4000  # characters of issue body kept for theming
SEARCH_CAP = 1000  # GitHub search returns at most this many results per query
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# (url, headers) -> (status, response headers, body bytes)
HttpGet = Callable[[str, dict], tuple[int, dict, bytes]]


class GitHubError(RuntimeError):
    pass


def urllib_get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def default_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "support-trends-report",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def window_start(weeks: int, now: datetime | None = None) -> str:
    """Start of the window: Monday 00:00 UTC, `weeks` full ISO weeks back including this one."""
    now = now or datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (monday - timedelta(weeks=weeks - 1)).strftime(TS_FMT)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FMT).replace(tzinfo=UTC)


def week_chunks(since: str, now: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end) per ISO week from `since` (a Monday) up to and including now's week."""
    start, chunks = _parse_ts(since), []
    while start <= now:
        chunks.append((start, start + timedelta(weeks=1)))
        start += timedelta(weeks=1)
    return chunks


def parse_issue(repo: str, raw: dict, fetched_at: str) -> dict | None:
    """Turn one issue item (search or issues API, same shape) into a row. None for PRs."""
    if "pull_request" in raw:
        return None
    reactions = raw.get("reactions") or {}
    user = raw.get("user") or {}
    labels = []
    for label in raw.get("labels") or []:
        # The API returns label objects; older payloads may use plain strings.
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            labels.append(name)
    return {
        "repo": repo,
        "number": int(raw["number"]),
        "title": (raw.get("title") or "").strip(),
        "body": (raw.get("body") or "")[:BODY_LIMIT],
        "state": raw.get("state", "open"),
        "labels": sorted(labels),
        "comments": int(raw.get("comments") or 0),
        "reactions": int(reactions.get("total_count") or 0),
        "thumbs_up": int(reactions.get("+1") or 0),
        "author": user.get("login"),
        "created_at": raw["created_at"],
        "updated_at": raw.get("updated_at") or raw["created_at"],
        "closed_at": raw.get("closed_at"),
        "html_url": raw.get("html_url") or f"https://github.com/{repo}/issues/{raw['number']}",
        "fetched_at": fetched_at,
    }


def search_url(repo: str, start: datetime, end: datetime, page: int, per_page: int) -> str:
    last = end - timedelta(seconds=1)  # search ranges are inclusive
    q = f"repo:{repo} is:issue created:{start.strftime(TS_FMT)}..{last.strftime(TS_FMT)}"
    params = {"q": q, "sort": "created", "order": "asc", "per_page": per_page, "page": page}
    return f"{API}/search/issues?{urllib.parse.urlencode(params)}"


@dataclass
class FetchResult:
    weeks_read: int = 0
    weeks_skipped: int = 0          # already done in an earlier run
    requests: int = 0
    issues: int = 0
    skipped_prs: int = 0
    splits: int = 0                 # ranges split because they had > SEARCH_CAP results
    warnings: list[str] = field(default_factory=list)


def fetch_issues(
    conn: sqlite3.Connection,
    repo: str,
    since: str,
    http_get: HttpGet = urllib_get,
    per_page: int = 100,
    record_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    refresh: bool = False,
    cap: int = SEARCH_CAP,
    clock: Callable[[], float] = time.time,
) -> FetchResult:
    now = now or datetime.now(UTC)
    fetched_at = now.strftime(TS_FMT)
    headers = default_headers()
    result = FetchResult()
    no_reactions = False

    def get_page(start: datetime, end: datetime, page: int) -> dict:
        url = search_url(repo, start, end, page, per_page)
        body = _get_with_retry(http_get, url, headers, sleep, clock)
        result.requests += 1
        if record_dir:
            record_dir.mkdir(parents=True, exist_ok=True)
            name = f"search_{start:%Y%m%dT%H%M%S}_{end:%Y%m%dT%H%M%S}_p{page}.json"
            (record_dir / name).write_bytes(body)
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise GitHubError(f"unexpected search response for {url}: {str(data)[:200]}")
        if data.get("incomplete_results"):
            result.warnings.append(
                f"GitHub marked results for {start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M} "
                "as incomplete (search timed out); rerun with --refresh later")
        return data

    def store(items: list) -> None:
        nonlocal no_reactions
        for raw in items:
            if "reactions" not in raw:
                no_reactions = True
            row = parse_issue(repo, raw, fetched_at)
            if row is None:
                result.skipped_prs += 1
                continue
            db.upsert_issue(conn, row)
            result.issues += 1

    def fetch_range(start: datetime, end: datetime) -> None:
        first = get_page(start, end, 1)
        total = int(first.get("total_count") or 0)
        if total > cap:
            if end - start <= timedelta(minutes=1):
                raise GitHubError(f"more than {cap} issues in one minute at {start}; "
                                  "cannot split further")
            result.splits += 1
            mid = start + (end - start) / 2
            mid = mid.replace(microsecond=0)
            fetch_range(start, mid)
            fetch_range(mid, end)
            return
        store(first["items"])
        pages = -(-total // per_page)
        for page in range(2, pages + 1):
            items = get_page(start, end, page)["items"]
            if not items:
                break
            store(items)

    for start, end in week_chunks(since, now):
        key = (repo, start.strftime(TS_FMT), end.strftime(TS_FMT))
        row = conn.execute("SELECT done FROM fetch_chunks WHERE repo = ? AND start_at = ? "
                           "AND end_at = ?", key).fetchone()
        if row and row["done"] and not refresh:
            result.weeks_skipped += 1
            continue
        fetch_range(start, end)
        done = int(end <= now)  # the current week keeps changing; re-read it next time
        conn.execute(
            "INSERT INTO fetch_chunks (repo, start_at, end_at, done, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo, start_at, end_at) DO UPDATE SET "
            "done=excluded.done, updated_at=excluded.updated_at", (*key, done, fetched_at))
        conn.commit()  # issues and the checkpoint for this week land together
        result.weeks_read += 1

    if no_reactions:
        result.warnings.append("some search results had no 'reactions' field; those issues "
                               "are stored with 0 reactions, so most-reacted lists rank them last")
    return result


def _get_with_retry(http_get: HttpGet, url: str, headers: dict, sleep, clock,
                    attempts: int = 4) -> bytes:
    status = None
    for attempt in range(attempts):
        status, resp_headers, body = http_get(url, headers)
        if status == 200:
            return body
        lower = {k.lower(): v for k, v in resp_headers.items()}
        if status in (403, 429):
            if "retry-after" in lower:  # secondary rate limit
                sleep(int(lower["retry-after"]) + 1)
                continue
            if lower.get("x-ratelimit-remaining") == "0":
                reset = int(lower.get("x-ratelimit-reset", "0"))
                wait = reset - clock()
                if wait <= 120:  # the search limit resets every minute; just wait
                    sleep(max(wait, 0) + 1)
                    continue
                raise GitHubError(
                    f"GitHub rate limit reached (resets at epoch {reset}). Set GITHUB_TOKEN "
                    "for a higher limit, then rerun; finished weeks are not fetched again.")
        if status == 422:
            raise GitHubError(
                f"GitHub refused the query (HTTP 422): {body[:300]!r}. URL: {url}")
        if status >= 500:
            sleep(2 ** (attempt + 1))
            continue
        raise GitHubError(f"GET {url} -> HTTP {status}: {body[:200]!r}")
    raise GitHubError(f"GET {url} failed after {attempts} attempts (last HTTP {status})")
