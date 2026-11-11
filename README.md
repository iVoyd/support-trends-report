# support-trends-report

A weekly "what's breaking" report for an open-source project's GitHub issues. It groups
recent issues into themes, shows which themes are rising, and turns the rising ones into
recommendations that link to the issues they are based on. It only reads from GitHub; it
never posts, comments or labels.

Default target: `supabase/supabase`. Use `--repo owner/name` for another project.
Full plan: [SPEC.md](SPEC.md).

## Status

| Milestone | State |
|---|---|
| 1. Data + themes | Done. 14 weeks (2026-W32 to W45) of supabase/supabase issues fetched and themed on PC1: 359 issues, 0 failures. `themes.yaml` is at v2. |
| 2. Trends + recommendations | Done. `trends report --week 2026-W44` ran on PC1 against the full history. |
| 3. HTML report + polish | Done. HTML report with an 8-week chart, number checks, README. Three sample reports (W36, W43, W44) committed as Markdown and HTML, from PC1, on themes v2. |

## How it works

```
GitHub REST API ──► trends fetch ──► SQLite (data/trends.db)
 (read-only GET)     resumable          │
                                        ▼
                trends seed-themes ──► themes.yaml  (run once, review, commit; frozen)
                (labels + sample → LLM)  │
                                        ▼
                     trends theme ──► one theme per issue, validated, stored per
                     (LLM, batches,      themes.yaml version; tokens + cost per run
                      cached prompt)     │
                                        ▼
                     trends status ──► issues / themed per ISO week, LLM spend
                                        │
                                        ▼
                    trends report ──► weekly counts, week-over-week change, rising
                    --week 2026-W44    themes, most-reacted issues; LLM writes 1-2
                                       actions per top-3 rising theme, citations
                                       checked (saved, reused on rerun)
                                        │
                                        ▼
                     reports/2026-W44.md + reports/2026-W44.html (8-week chart)
                                        │
                                        ▼
                trends check-report ──► every number re-checked against SQLite
```

- **fetch** uses the GitHub search API, one query per ISO week:
  `repo:OWNER/NAME is:issue created:START..END`. The window starts Monday 00:00 UTC,
  `--weeks` ISO weeks back including the current one. It stores title, body (first 4,000
  characters), labels, state, comment count and reactions. Search returns at most 1,000
  results per query, so a week with more is split in half until each piece fits. Each
  finished week is checkpointed, so an interrupted fetch resumes at the first week not yet
  done. The current week is re-read on every run; `--refresh` re-reads every week to update
  state, comments and reactions.
  (The first version paged `/repos/{repo}/issues`. On supabase/supabase that endpoint
  includes pull requests and returns HTTP 422 past page 10, so it could not load 8 weeks.)
- **seed-themes** sends the label counts and a sample of 80 issue titles to the model and
  asks for 8-15 non-overlapping themes, then adds `other`. The result goes to
  `themes.yaml`. Review and edit it, then commit it.
- **theme** themes every stored issue that has no theme yet (`--weeks N` limits it to the
  last N weeks). It sends issues in batches of 20. The system prompt (rules + theme list) is the
  same for every batch, so it is prompt-cached. The answer is constrained by a JSON schema
  whose `theme` field is an enum of the theme ids, and then validated in code: every issue
  in the batch must come back exactly once with a known theme. Issues that fail are retried
  one at a time; if they still fail they stay unthemed and are listed. Nothing is guessed.
- Tokens (input, output, cache write, cache read) and cost are written to the `llm_runs`
  table for every run, including runs that fail part-way.
- **report** counts issues per theme for the target ISO week and the 4 weeks before it.
  A theme is *rising* when it has at least 5 issues this week and at least 2x its average
  over those 4 weeks. For the top 3 rising themes (skipping `other`), the model gets that
  theme's issues for the week, with URLs, and writes 1-2 actions: a docs fix, a bug to
  prioritise, or an FAQ entry. Each action must cite issue URLs from that input. The code
  checks every citation; on a problem the model gets one retry with the list of problems,
  and any action that still cites an unknown URL (or nothing) is dropped and noted in the
  report. `--no-llm` writes the numbers only. Recommendations are saved per week and
  theme, so running the report again reuses them at no cost (`--regenerate` asks again).
- The report is written twice from the same numbers: Markdown, and a single static HTML
  file (no JavaScript, no external files, light and dark). The HTML adds a chart of theme
  counts over the 8 weeks ending with the report week: one row of bars per theme, all on
  one scale, the report week in a stronger blue, a "Rising" label on rising themes, and a
  tooltip on each bar. Weeks with no stored issues are named under the chart, and so are
  weeks that have issues without a theme (their counts would otherwise look too low);
  `report` also prints a warning for those.
- After writing, `report` re-checks its numbers against the database and exits with code
  3 if anything differs. `trends check-report reports/2026-W44.md` runs the same check
  later; `--internal-only` checks a report against itself (its "All themes" table must
  agree with its "Weekly counts" table), which needs no database. CI runs that on every
  committed report.

### How themes stay stable

Week-over-week numbers only mean something if every week is themed against the same list.
`trends theme` reads `themes.yaml` and never changes it. Assignments are stored with the
file's `version`. If you change the list, bump `version`; `trends theme` then re-themes
every issue in the window against the new list, and the old assignments stay in the
database for comparison. `trends compare-themes` then shows per-theme counts under both
versions and which issues moved (`--week` for one week, `--list` for every moved issue).

## Run it

Needs Python 3.11+. On the PC1 VM:

```bash
git clone https://github.com/iVoyd/support-trends-report.git && cd support-trends-report
```

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

Load the API key (it is read from the environment only) and, optionally, a GitHub token.
8 weeks of supabase issues takes about 8-16 search requests. The search API allows 10
requests a minute without `GITHUB_TOKEN` and 30 with one; fetch waits out a short limit
by itself, but a token makes it faster:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # or source your own env file; never commit it
```

```bash
export GITHUB_TOKEN=$(gh auth token)   # optional; any read-only token works
```

Then:

```bash
trends fetch --repo supabase/supabase --weeks 10
```

Ten weeks: the report compares a week with the 4 before it, and the HTML chart shows the
8 weeks ending with the report week, so a report for last week needs about 9-10 weeks of
history.

```bash
trends seed-themes
```

Read `themes.yaml`, edit names/descriptions if needed, and commit it. Then check the cost
estimate, try a small batch, and run the rest:

```bash
trends theme --estimate && trends theme --limit 40
```

```bash
trends theme && trends status
```

`trends status` prints issues and themed issues for every stored week; the two numbers
should be equal. Then the weekly report:

```bash
trends report --week 2026-W44
```

That writes `reports/2026-W44.md` and `reports/2026-W44.html` (default week: the last full
week) and checks the numbers. Other commands:

- `trends check-report reports/2026-W44.md` re-checks a report against the database.
- `trends compare-themes --week 2026-W36` compares theme assignments between the previous
  and current themes.yaml version.
- `trends stability` themes stored issues twice (or `--passes N` times) without saving and
  prints how many got the same theme every time. `--sample N` limits the issues,
  `--shuffle` puts each issue in different batches on later passes (to see whether its
  neighbours in a batch sway it), and `--list` names every issue that changed.
- `trends theme` and `trends stability` take `--effort low|medium|high|xhigh|max` (default
  low) and `--thinking disabled|adaptive` (default disabled). Thinking disabled needs
  effort high or below. There is no temperature option: see Limitations.

- `trends fetch --record-dir tests/fixtures/github_live` also saves the raw API pages, if
  you want real fixtures.
- `trends import-triage path/to/triage.db` imports issue number, type and area from
  support-triage-assistant's SQLite file (any table with an `issue_number`/`number` column
  plus `type` and/or `area`). Optional; nothing depends on it yet.
- `--db PATH` before the command uses another database file (default `data/trends.db`).

Tests (no network, no API key):

```bash
ruff check . && pytest
```

### Weekly run with Prefect (optional)

The three weekly commands can also run as one [Prefect](https://www.prefect.io) flow
(`src/trends/flow.py`), with a schedule, retries and a run history:

```
fetch (3 retries) ──► theme (1 retry) ──► report (no retry; fails if its numbers don't match the DB)
```

- **fetch** is retried because GitHub's search API rate-limits and times out; it is
  resumable, so a retry only re-reads weeks that didn't finish.
- **theme** is retried once; it only themes issues that are still unthemed.
- **report** is not retried. If `check-report` finds a number that doesn't match the
  database, the run fails instead of publishing a wrong report.

Each task calls the same functions as the CLI commands, so the flow adds scheduling and
retries, not new logic.

```bash
pip install -e ".[prefect]"
python -m trends.flow                          # run once (last full week)
python -m trends.flow --no-llm                 # run once, no recommendation calls
prefect server start                           # optional: the UI at http://127.0.0.1:4200
python -m trends.flow --serve "15 7 * * 1"     # Mondays 07:15 UTC, until stopped
```

`tests/test_flow.py` checks the order of the steps, that a failed fetch is retried, and
that a failed report check fails the run.

## Sample report

Three real reports are committed, each as Markdown and HTML, all on themes.yaml v2:
2026-W36 ([md](reports/2026-W36.md), [html](reports/2026-W36.html)),
2026-W43 ([md](reports/2026-W43.md), [html](reports/2026-W43.html)) and
2026-W44 ([md](reports/2026-W44.md), [html](reports/2026-W44.html)).
GitHub shows the HTML as source; download it or open it locally to see the chart.

From 2026-W36 (supabase/supabase, 60 issues), the week of the sign-in incident below:

> **Rising themes**
> - **Dashboard account access, MFA and billing**: 33 issues, 4-week average 4.8 (6.9x)
> - **SQL Editor and Table Editor**: 5 issues, 4-week average 2.5 (2.0x)
>
> **Recommendations: Dashboard account access, MFA and billing**
> - **Bug triage priority: Prioritise the auth.supabase.io 301 redirect loop affecting
>   dashboard sign-in.** Many reports describe ERR_TOO_MANY_REDIRECTS and 'Failed to
>   fetch (auth.supabase.io)' on the dashboard sign-in, with a CORS preflight returning
>   HTTP 301 to itself, starting around 1 Sep 2026. Treat this as one incident and link
>   duplicates to a single tracking issue, since it blocks users from reaching support
>   tickets. Based on: #49847, #49816, #49822, #49827, #49812
> - **FAQ entry: Dashboard login fails with redirects or 'Failed to fetch' on
>   auth.supabase.io.** Support should tell users this is a sign-in service problem, not
>   an error in their project, and to check status.supabase.com and retry later in a
>   fresh browser session. Based on: #49819, #49814, #49827, #49910

The full report also has a table of all 14 themes (this week, last week, change, 4-week
average), the most-reacted issues per theme, and 5 weeks of counts; the HTML adds the
8-week chart. 2026-W44 has one rising theme (documentation corrections, with two docs
fixes). 2026-W43 has none on v2; on v1 "Support requests and unclear reports" just
cleared the rule with 5 issues, and v2 puts 4 issues there.

## What the report caught

2026-W36 (31 Aug - 6 Sep) has 60 issues, two to three times a normal week.

Reading the issues shows one incident. On Tuesday 1 September, about 20 issues were
opened about the dashboard sign-in failing, most with the same error: auth.supabase.io
"redirected you too many times" (ERR_TOO_MANY_REDIRECTS), for GitHub, Google and password
login alike. Issue 49847 reports it starting around 09:00 UTC and reproducing from two
regions. Reports kept coming for the rest of the week, along with related failures
(GoTrue returning 500, a 2FA lockout, failed password-reset emails).

Examples: 49847, 49834, 49814, 49828, 49822 (redirect loop); 49838, 49837, 49832 (GitHub
and Google sign-in); 49811, 49809 (password reset); 49890 (GoTrue 500); 49931 (2FA).

The W36 report, with the 4 weeks before it as history, flagged it. On themes.yaml v1:

| Rising theme (v1) | W36 issues | 4-week average | vs average |
|---|---:|---:|---:|
| Account access, MFA and billing | 24 | 4.2 | 5.6x |
| Auth tokens, passwords and API keys | 14 | 4.0 | 3.5x |
| SQL Editor and Table Editor | 5 | 2.2 | 2.2x |

About 32 of the 38 issues in the first two themes are the sign-in incident; the other 6
are unrelated (an org invite, two auth config bugs, an invoice, a project deletion
question, an empty report). The third theme only just clears the rule (5 issues, 2.2x).

On v2 (below), the incident sits in one theme: "Dashboard account access, MFA and
billing" has 33 issues at 6.9x its average, and the report's first recommendation says
to treat the reports as one incident and link the duplicates to a single tracking issue.

What this shows, and what it does not:

- Weekly themed counts and the rising rule caught a one-day incident.
- The tool groups issues by theme; it does not detect incidents. On v2 the
  recommendation step, which reads the issues of one rising theme together, did describe
  them as one incident.
- On v1 the incident was split across two themes, which made each look smaller than it
  was. That led to v2.

### What I changed and why: themes.yaml v1 to v2

In v1 the same failure landed in two themes. 49814 and 49834 have identical titles
("auth.supabase.io redirected you too many times"); one went to account access, the
other to auth. The v1 descriptions overlapped: account access covered "login lockouts",
auth covered "Supabase Auth behaviour", and "I can't sign in to the dashboard" fits both.

v2 (decided by Theo) draws the line by whose login it is:

- **Dashboard account access, MFA and billing**: signing in to or managing the Supabase
  dashboard account (login, MFA, billing, org access).
- **Supabase Auth for apps, tokens and API keys**: the Auth product that users' own apps
  use (tokens, JWTs, providers, SMTP, hooks), plus project API keys and pooler
  authentication, which v1 already put there.

The theme ids did not change, so `trends compare-themes` can show what moved. All 359
stored issues were re-themed under v2 ($0.0173); v1 assignments stay in the database.

W36, v1 vs v2 (`trends compare-themes --week 2026-W36`): 13 of 60 issues moved.

| Theme (v2 name) | v1 | v2 | Change |
|---|---:|---:|---:|
| Dashboard account access, MFA and billing | 24 | 33 | +9 |
| Supabase Auth for apps, tokens and API keys | 14 | 5 | -9 |
| SQL Editor and Table Editor | 5 | 5 | 0 |
| API gateway, PostgREST and Data API | 4 | 4 | 0 |
| Database availability and restarts | 4 | 4 | 0 |
| Support requests and unclear reports | 3 | 4 | +1 |
| Dashboard and Studio UI | 2 | 3 | +1 |
| Documentation and content corrections | 3 | 2 | -1 |
| Other | 1 | 0 | -1 |
| **Total** | 60 | 60 | |

Nine of the 13 moves are the intended fix: dashboard sign-in reports going from Auth to
account access (49820, 49832, 49834, 49837, 49838, 49847, 49907, 49929, 49944). One more
sign-in report (49826, "This page isn't working") moved there from support requests.
The other three are small reshuffles (a pricing-page crash to Studio UI, a project
deletion question and an unclear report to support requests).

Across all 359 stored issues, 44 moved (12%). 12 went from Auth to account access and 3
more into account access from other themes. The remaining 29 moved between themes
whose descriptions did not change, for example database availability and API gateway in
both directions. That is run-to-run variation in the model's choice on borderline issues
(about 8% of issues), not an effect of v2; see Limitations.

## Results

Runs on PC1, 2026-11-03, supabase/supabase, themes.yaml v2 (14 themes):

| ISO week | Issues | Themed |
|---|---:|---:|
| 2026-W32 | 31 | 31 |
| 2026-W33 | 16 | 16 |
| 2026-W34 | 27 | 27 |
| 2026-W35 | 25 | 25 |
| 2026-W36 | 60 | 60 |
| 2026-W37 | 18 | 18 |
| 2026-W38 | 28 | 28 |
| 2026-W39 | 24 | 24 |
| 2026-W40 | 17 | 17 |
| 2026-W41 | 25 | 25 |
| 2026-W42 | 29 | 29 |
| 2026-W43 | 27 | 27 |
| 2026-W44 | 29 | 29 |
| 2026-W45 (partial) | 3 | 3 |
| **Total** | **359** | **359** |

- fetch: one search request per week, no warnings (search results include reactions).
- theme: 0 issues left unthemed under v2 (one pass over all 359 issues: 18 calls).
- Prompt caching works: the ~1,960-token theme prompt was written to the cache once and
  read on the other 17 calls of the v2 pass (33,320 cache-read tokens).
- reports on v2: W36 has 2 rising themes, W43 none, W44 one.

| LLM step | Runs | Calls | Cost (USD) |
|---|---:|---:|---:|
| seed-themes | 1 | 1 | 0.0010 |
| theme (v1 passes, then all 359 on v2) | 6 | 37 | 0.0353 |
| report (W36, W43, W44 on v1 and v2, incl. reruns) | 8 | 11 | 0.0053 |
| stability experiments (4 runs, see Limitations) | 4 | 800 | 0.1355 |
| **Total** | | | **0.1771** |

The first fetch used `/repos/{repo}/issues` and stopped at page 11 with HTTP 422, after
reading 107 issues (W41-W45). That is why fetch now uses the search API.

## Cost per week

Model: `claude-haiku-5-5` ($0.10 per million input tokens, $0.50 per million output;
cache writes 1.25x input, cache reads 0.1x input). GitHub reads are free.

| Step, one normal week | Measured basis | Cost (USD) |
|---|---|---:|
| Theme ~25 new issues | 359 issues cost $0.0173 on v2 | ~0.0012 |
| Report, 1-3 recommendation calls | $0.0004 per call (W44) | 0.0004-0.0012 |
| **Week** | | **~0.002-0.003** |

That is well under $0.20 a year. Seeding the theme list is a one-off ($0.0010). Rerunning
a report for the same week costs nothing, since recommendations are reused.
`trends theme --estimate` prints an estimate for your data before you spend anything; for
the v2 pass it guessed $0.0124 against $0.0173 actual (about 30% low), so treat it as a
lower bound. Changing the theme list means re-theming every stored issue; for 14 weeks
of supabase issues that was under $0.02.

## Limitations

- The tests use synthetic GitHub data and a fake model, so they run with no network. The
  real APIs have been used only in the PC1 runs above.
- One theme per issue. Issues that span two areas get the closest one.
- Theming is not perfectly repeatable. Measured with `trends stability` on PC1
  (2026-11-03, themes v2, all 359 stored issues, two passes each):

  | Setting | Issues | Same theme both passes |
  |---|---:|---:|
  | Defaults (effort low, thinking disabled), same batches | 359 | 93.3% (24 changed) |
  | Defaults, different batches (`--shuffle`) | 359 | 86.6% (48 changed) |
  | Effort medium, thinking adaptive, same batches | 100 (sample) | 95.0% (5 changed) |
  | Defaults, one issue per call (`--batch-size 1`) | 359 | 95.5% (16 changed) |

  So about 7% of issues change theme on a plain rerun, and about 13% when each issue sits
  in a different batch of 20: the other issues in a batch sway the model about as much as
  its own variation does. The issues that move are borderline ones, mostly between docs
  and support requests, account access and Auth, Auth and Studio UI, and the API gateway
  and Auth. Adaptive thinking was not clearly better (95% on 100 issues is within
  sampling noise of 93%), cost about 1.6x per issue, and its answers matched the saved
  default-setting themes for only 85% of issues, so switching settings would itself move
  about 15% of assignments. One issue per call removes the batch-neighbour effect and
  reached 95.5%: better, but short of the 97% set in advance as the bar for switching
  (at least half the disagreements gone). It made 718 calls instead of 36, cost 1.5x,
  and would have moved 15% of saved assignments (pass 1 matched them for 85.2%). The
  defaults stay: batches of 20, effort low, thinking disabled.

  What this means for the report: a theme's weekly count can move by an issue or two
  between runs. The rising rule (at least 5 issues and 2x the 4-week average) absorbs
  most of that, but a theme right at the threshold can flip, as W43 did between v1 and
  v2. Read a rising theme near 5 issues as a maybe. The earlier "8% moved between
  unchanged themes" in the v1-to-v2 comparison is this same variation, mostly from
  different batching.
- Temperature 0 is not available. Claude Haiku 5.5 rejects any non-default
  `temperature` (HTTP 400), and API version 2026-10-21, which the installed SDK sends,
  rejects `temperature` on every model; the SDK no longer has the parameter. Getting it
  back would mean an older SDK, an older API version and an older model. Even then,
  temperature 0 does not guarantee identical answers. The settings that remain are
  `--effort` and `--thinking`.
- Themes whose descriptions overlap split one problem across two themes. Check rising
  themes for this, and fix it in `themes.yaml` (as v2 did for sign-in).
- Theme quality depends on the seeded list; review `themes.yaml` before theming.
- Prompt caching only applies once the system prompt is at least 512 tokens. With a short
  theme list it may fall below that and simply not cache; the run output shows cache-read
  tokens, so you can see which happened. At Haiku prices the difference is cents.
- Finished weeks are not re-read unless you pass `--refresh`, so their state, comments and
  reactions are as of the fetch that finished them.
- Search results that lack a `reactions` field are stored with 0 reactions; fetch prints a
  warning if that happens.
- `import-triage` assumes the triage database covers one repository.
- The rising rule is deliberately simple. A theme that is steadily large never "rises",
  and a quiet theme can rise on 5 issues.
- The chart uses one scale for all rows, so a single big week (like W36's 60 issues)
  flattens the other bars. The numbers are in the tables and tooltips.
- The chart covers the 8 weeks ending with the report week. Weeks before the fetched
  window show as empty and are named under the chart; fetch 10 weeks to avoid that.
- Counts include themed issues only. After fetching more weeks, run `trends theme` before
  `trends report`; the report warns about any week in it with unthemed issues.
- Recommendations come from the cheapest model and only see one week of one theme. They
  are a starting point for a human, not a decision. The citation check proves the cited
  issues exist in the input, not that they support the action.

## How this was built

Written with AI assistance (Claude Code), reviewed by Theo O'Ferrall. Design choices
follow [SPEC.md](SPEC.md) and the session rules in [CLAUDE.md](CLAUDE.md).
