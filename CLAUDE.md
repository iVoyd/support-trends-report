# Session rules

- Owner: Theo O'Ferrall. Commit as `Theo O'Ferrall <theodore.ofe@gmail.com>` with the
  co-author and session trailers your environment provides. Work on branches, open PRs,
  never force-push `main`. Check a PR is still open before pushing to its branch.
- Keep it simple and explainable: plain Python 3.11+, small modules, few dependencies,
  no framework unless the spec asks for one. Prefer readable code over clever code.
- Tests: pytest, no network in tests (use recorded fixtures). CI on GitHub Actions runs
  `ruff` and `pytest`.
- Secrets: read API keys from environment variables only (`ANTHROPIC_API_KEY`,
  `GITHUB_TOKEN`). Never commit `.env`, keys, or tokens. Add `.env` to `.gitignore`.
- Read-only toward the outside world: never post, comment, label, or write to any
  third-party service.
- Costs: default to the cheapest capable model (`claude-haiku-5-5`), use prompt caching
  for long fixed prompts, and record tokens and cost per run.
- README is part of "done": what it does, a diagram, how to run it, results/scorecard,
  limitations, and a short "how this was built" note (AI-assisted, reviewed by Theo).
- Write plainly in docs and commit messages. No hype.
