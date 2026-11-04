"""Shared test fakes. No test touches the network."""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trends import db
from trends.llm import Usage

FIXTURES = Path(__file__).parent / "fixtures"
GITHUB_PAGES = FIXTURES / "github"
REPO = "supabase/supabase"
FIXED_NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)  # Tuesday of 2026-W45


def fixture_items(pages_dir: Path = GITHUB_PAGES) -> list[dict]:
    files = sorted(pages_dir.glob("issues_page_*.json"), key=lambda p: int(p.stem.split("_")[-1]))
    return [item for p in files for item in json.loads(p.read_text())]


class FakeGitHub:
    """Serves the fixture issues like GET /search/issues does.

    Parses `repo:`, `is:issue` and `created:A..B` (inclusive) from q, sorts by created_at
    ascending, slices by per_page/page, and, like GitHub, answers HTTP 422 for any page
    past the first `cap` results. `fail_on` maps a request number (1-based) to a status to
    return once instead, to simulate errors mid-fetch.
    """

    def __init__(self, pages_dir: Path = GITHUB_PAGES, fail_on: dict[int, int] | None = None,
                 cap: int = 1000, drop_fields: tuple[str, ...] = ()):
        self.items = fixture_items(pages_dir)
        for item in self.items:
            for f in drop_fields:
                item.pop(f, None)
        self.fail_on = dict(fail_on or {})
        self.cap = cap
        self.urls: list[str] = []
        self.queries: list[str] = []

    def matching(self, q: str) -> list[dict]:
        repo = re.search(r"repo:(\S+)", q).group(1)
        lo, hi = re.search(r"created:(\S+)\.\.(\S+)", q).groups()
        out = [i for i in self.items
               if repo == REPO and lo <= i["created_at"] <= hi
               and not ("is:issue" in q and "pull_request" in i)]
        return sorted(out, key=lambda i: i["created_at"])

    def __call__(self, url: str, headers: dict):
        self.urls.append(url)
        assert "Authorization" not in headers or headers["Authorization"].startswith("Bearer ")
        parts = urllib.parse.urlsplit(url)
        assert parts.path == "/search/issues", url
        params = dict(urllib.parse.parse_qsl(parts.query))
        if len(self.urls) in self.fail_on:
            return self.fail_on.pop(len(self.urls)), {}, b'{"message": "Server Error"}'
        page, per_page = int(params["page"]), int(params["per_page"])
        if page * per_page > self.cap:
            return 422, {}, json.dumps({"message": "Only the first 1000 search results are "
                                        "available"}).encode()
        self.queries.append(params["q"])
        items = self.matching(params["q"])
        body = {"total_count": len(items), "incomplete_results": False,
                "items": items[(page - 1) * per_page: page * per_page]}
        return 200, {}, json.dumps(body).encode()


SEED_THEMES = [
    {"id": "auth", "name": "Auth", "description": "Sign-in, sessions, OAuth, OTP.",
     "labels": ["auth", "not-a-real-label"]},
    {"id": "realtime", "name": "Realtime", "description": "Channels, presence, broadcast.",
     "labels": ["realtime"]},
    {"id": "storage", "name": "Storage", "description": "Buckets, uploads, signed URLs.",
     "labels": ["storage"]},
    {"id": "edge-functions", "name": "Edge Functions", "description": "Deploys, cold starts.",
     "labels": ["edge-functions"]},
    {"id": "local-dev-cli", "name": "Local dev and CLI", "description": "supabase CLI.",
     "labels": ["cli"]},
    {"id": "database", "name": "Database", "description": "Postgres, migrations, pooler.",
     "labels": ["database"]},
    {"id": "dashboard", "name": "Dashboard", "description": "Studio UI.", "labels": ["studio"]},
    {"id": "docs", "name": "Docs", "description": "Wrong or missing docs.",
     "labels": ["documentation"]},
]
LABEL_TO_THEME = {lbl: t["id"] for t in SEED_THEMES for lbl in t["labels"]}
TITLE_HINTS = {  # for unlabelled issues
    "Magic link": "auth", "signIn": "auth", "Session": "auth", "OTP": "auth", "Auth": "auth",
    "Realtime": "realtime", "Presence": "realtime", "Broadcast": "realtime",
    "Postgres changes": "realtime", "Upload": "storage", "Signed URL": "storage",
    "Image": "storage", "Resumable": "storage", "Storage": "storage", "Edge": "edge-functions",
    "Deno": "edge-functions", "CORS": "edge-functions", "supabase ": "local-dev-cli",
    "Local studio": "local-dev-cli", "Migration": "database", "pg_cron": "database",
    "pooler": "database", "Generated types": "database", "Table editor": "dashboard",
    "SQL editor": "dashboard", "Dashboard": "dashboard", "Docs": "docs", "docs": "docs",
}


class FakeLLM:
    """Answers like the real model would, from labels and title keywords.

    `drop` lists issue numbers to leave out of the first answer that contains them, to
    exercise the retry path. `bad_theme` lists numbers to answer with an invented theme.
    """

    model = "claude-haiku-5-5"

    def __init__(self, drop=(), bad_theme=(), always_drop=(), bad_citations=0):
        self.bad_citations = bad_citations  # recommendation answers to spoil with a fake URL
        self.drop = set(drop)
        self.bad_theme = set(bad_theme)
        self.always_drop = set(always_drop)
        self.calls: list[dict] = []

    def call(self, system, user, schema, max_tokens=4096):
        self.calls.append({"system": system, "user": user, "schema": schema})
        usage = Usage(input_tokens=len(user) // 4, output_tokens=30, calls=1,
                      cache_write_tokens=0 if len(self.calls) > 1 else len(system) // 4,
                      cache_read_tokens=len(system) // 4 if len(self.calls) > 1 else 0)
        if "themes" in schema["properties"]:
            return {"themes": json.loads(json.dumps(SEED_THEMES))}, usage
        if "actions" in schema["properties"]:
            urls = re.findall(r"^- (https://\S+)$", user, re.M)
            cites = urls[:2]
            if self.bad_citations:
                self.bad_citations -= 1
                cites = cites + [f"https://github.com/{REPO}/issues/999999"]
            theme = re.search(r"^Theme: .*\((\S+)\)", user, re.M).group(1)
            return {"actions": [
                {"kind": "triage_priority", "title": f"Prioritise the top {theme} bug",
                 "detail": "Several reports describe the same failure.", "citations": cites},
                {"kind": "faq_entry", "title": f"Add an FAQ entry for {theme}",
                 "detail": "Users ask the same question.", "citations": urls[-1:]},
            ]}, usage
        out = []
        for m in re.finditer(r"^### #(\d+): (.*)\nlabels: (.*)$", user, re.M):
            number, title, labels = int(m.group(1)), m.group(2), m.group(3).split(", ")
            if number in self.always_drop:
                continue
            if number in self.drop:
                self.drop.discard(number)
                continue
            theme = next((LABEL_TO_THEME[x] for x in labels if x in LABEL_TO_THEME), None)
            theme = theme or next((v for k, v in TITLE_HINTS.items() if k in title), "other")
            if number in self.bad_theme:
                self.bad_theme.discard(number)
                theme = "made-up-theme"
            out.append({"number": number, "theme": theme, "confidence": "high"})
        return {"assignments": out}, usage


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()
