"""Generate synthetic GitHub issue items for tests.

These are NOT recorded from GitHub. Each item has the documented shape of an issue object
(the same in the issues API and in search results), with supabase-like titles and labels,
so the tests can run with no network. tests/conftest.py's FakeGitHub serves them as
GET /search/issues responses. To record real search pages instead, run on a machine with
GitHub access: `trends fetch --record-dir tests/fixtures/github_live`.

Layout: ~11 weeks of issues ending Tuesday 2026-11-03, 50 items per page, a few pull
requests mixed in, and a storage spike in 2026-W44 (useful for the trends milestone).
Run: python tests/fixtures/make_github_fixtures.py
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "github"
REPO = "supabase/supabase"
END = datetime(2026, 11, 3, 18, 0, 0)
START = datetime(2026, 8, 24)  # Monday of 2026-W35, before the 8-week window

TOPICS = {
    "auth": (["auth"], [
        "Magic link redirects to localhost instead of site URL",
        "signInWithOAuth fails with PKCE flow on Safari",
        "Session not refreshed after token expiry",
        "Phone OTP not delivered for +44 numbers",
        "Auth hook returns 500 when custom claims added",
    ]),
    "realtime": (["realtime"], [
        "Realtime channel disconnects every 30s",
        "Postgres changes not received for filtered subscription",
        "Presence state out of sync after reconnect",
        "Broadcast messages dropped under load",
    ]),
    "storage": (["storage"], [
        "Upload fails with 413 for files over 50MB",
        "Signed URL returns 400 after bucket rename",
        "Image transformation returns blank image",
        "Resumable upload stalls at 99%",
        "Storage RLS policy blocks public bucket reads",
    ]),
    "functions": (["edge-functions"], [
        "Edge function cold start over 5 seconds",
        "Deno import map not respected on deploy",
        "Edge function logs missing in dashboard",
        "CORS preflight fails for edge function",
    ]),
    "cli": (["cli"], [
        "supabase start fails on Apple silicon",
        "supabase db reset hangs on seed file",
        "Local studio cannot connect after upgrade",
        "supabase link errors with invalid project ref",
    ]),
    "database": (["database"], [
        "Migration with enum change fails on remote",
        "pg_cron job runs twice after restart",
        "Connection pooler timeout under 100 clients",
        "Generated types miss view columns",
    ]),
    "dashboard": (["studio"], [
        "Table editor freezes on large JSON column",
        "SQL editor loses query on tab switch",
        "Dashboard shows wrong usage numbers",
    ]),
    "docs": (["documentation"], [
        "Docs: RLS example for multi-tenant is wrong",
        "Docs: missing guide for Next.js app router auth",
        "Typo in storage CDN docs",
    ]),
}
STATUS_LABELS = ["bug", "needs-triage", "question", "enhancement"]


def week_monday(i: int) -> datetime:
    return START + timedelta(weeks=i)


def build() -> list[dict]:
    rng = random.Random(42)
    items, number = [], 20000
    weeks = (END - START).days // 7 + 1
    for w in range(weeks):
        for topic, (labels, titles) in TOPICS.items():
            n = rng.choice([1, 1, 2, 2, 3])
            if topic == "storage" and w == weeks - 2:  # 2026-W44 spike
                n = 12
            for _ in range(n):
                created = week_monday(w) + timedelta(seconds=rng.randrange(7 * 86400))
                if created > END:
                    continue
                number += 1
                title = rng.choice(titles)
                lbls = labels + rng.sample(STATUS_LABELS, rng.choice([0, 1, 1, 2]))
                if rng.random() < 0.1:  # some issues arrive unlabelled
                    lbls = []
                closed = rng.random() < 0.4
                items.append(issue(number, title, lbls, created, closed, rng))
                if rng.random() < 0.05:  # PRs show up in the issues API too
                    number += 1
                    pr = issue(number, f"fix: {title.lower()}", [], created, True, rng)
                    pr["pull_request"] = {
                        "url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
                    items.append(pr)
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(number, title, labels, created, closed, rng) -> dict:
    up = rng.choice([0, 0, 0, 1, 2, 5, 12])
    total = up + rng.choice([0, 0, 1, 3])
    return {
        "url": f"https://api.github.com/repos/{REPO}/issues/{number}",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "number": number,
        "title": title,
        "user": {"login": f"user{rng.randrange(1000)}", "type": "User"},
        "labels": [{"name": n, "color": "ededed"} for n in labels],
        "state": "closed" if closed else "open",
        "comments": rng.choice([0, 1, 2, 4, 9]),
        "created_at": ts(created),
        "updated_at": ts(created + timedelta(hours=rng.randrange(1, 200))),
        "closed_at": ts(created + timedelta(days=2)) if closed else None,
        "author_association": "NONE",
        "body": f"**Describe the bug**\n{title}.\n\n**To Reproduce**\n1. ...\n\n"
                "**Expected behavior**\nIt works.\n\n**System information**\n- OS: macOS",
        "reactions": {"total_count": total, "+1": up, "-1": 0, "laugh": 0, "hooray": 0,
                      "confused": total - up, "heart": 0, "rocket": 0, "eyes": 0},
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("issues_page_*.json"):
        old.unlink()
    items, per_page = build(), 50
    pages = [items[i:i + per_page] for i in range(0, len(items), per_page)]
    for i, page in enumerate(pages, start=1):
        (OUT / f"issues_page_{i}.json").write_text(json.dumps(page, indent=1) + "\n")
    print(f"{len(items)} items in {len(pages)} pages -> {OUT}")


if __name__ == "__main__":
    main()
