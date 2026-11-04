# SPEC: support-trends-report

A weekly "what's breaking" report for an open-source project's issue tracker: groups recent
issues into themes, shows which themes are rising, and turns them into concrete,
evidence-linked recommendations for product, docs and support. Read-only.

Default target: `supabase/supabase` (same as support-triage-assistant, so the two projects
tell one story). Configurable `--repo`.

## Architecture

```
fetch issues (GitHub REST, SQLite cache) -> theme each issue (LLM, structured output,
from a small evolving theme list) -> weekly counts + week-over-week change
-> recommendations (LLM, grounded in linked issues) -> report (Markdown + static HTML)
```

## Milestone 1: data + themes

- `trends fetch --repo supabase/supabase --weeks 8`: issues opened in the window, with
  labels, state, reactions, comment counts. SQLite cache, resumable.
- `trends theme`: LLM assigns each issue one theme from a theme list (seeded from GitHub
  labels + a first LLM pass over a sample, then frozen in `themes.yaml` so weeks compare
  fairly). Structured output; cheapest model; prompt caching; cost recorded.
- Optional: import triage records from support-triage-assistant's SQLite if present
  (shared fields: issue number, type, area).
- Done when: 8 weeks of issues are themed and tests cover fetch parsing and theme validation.

## Milestone 2: trends + recommendations

- Weekly counts per theme, week-over-week change, "rising" themes (simple rule, e.g.
  ≥ 2x the 4-week average and ≥ 5 issues), most-reacted issues per theme.
- Recommendations: for the top 3 rising themes, the LLM writes 1–2 actions each (docs fix,
  bug triage priority, FAQ entry), each citing the specific issue URLs it is based on.
  Validate that every citation is a real issue from the input.
- Done when: `trends report --week 2026-W44` writes `reports/2026-W44.md`.

## Milestone 3: report + polish

- Static HTML version with a simple chart (theme counts over 8 weeks) using a small
  inline SVG or a lightweight chart library; no server needed.
- A check that numbers in the report match the database (a test).
- README: what it does, sample report, how themes stay stable, limitations, cost per week.
- Done when: two sample weekly reports are committed and the README is complete.
