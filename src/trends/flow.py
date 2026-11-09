"""The weekly run as a Prefect flow (optional: `pip install -e ".[prefect]"`).

`trends fetch`, `trends theme` and `trends report` are run by hand today. This flow runs
them as three Prefect tasks, in order, so the weekly report can run on a schedule with
retries and a run history:

- fetch is retried (GitHub's search API rate-limits and times out now and then);
  it is resumable, so a retry only re-reads weeks that didn't finish;
- theme is retried too: it only themes issues that are still unthemed, so a retry
  picks up where a failed batch stopped;
- report is not retried: if its numbers don't match the database (`check failed`),
  the flow fails and says so, instead of publishing a wrong report.

Each task calls the same CLI functions as the commands, so the flow adds no new logic.

Run once:            python -m trends.flow
Serve on a schedule: python -m trends.flow --serve "15 7 * * 1"   (Mondays 07:15 UTC)
"""

from __future__ import annotations

import argparse

from prefect import flow, get_run_logger, task

from . import cli


class StepFailed(RuntimeError):
    pass


def _run(step: str, argv: list[str]) -> None:
    code = cli.main(argv)
    if code != 0:
        raise StepFailed(f"trends {step} exited with code {code}")


@task(retries=3, retry_delay_seconds=60, task_run_name="fetch")
def fetch(repo: str, weeks: int, db: str) -> None:
    _run("fetch", ["--db", db, "fetch", "--repo", repo, "--weeks", str(weeks)])


@task(retries=1, retry_delay_seconds=30, task_run_name="theme")
def theme(repo: str, weeks: int, db: str) -> None:
    _run("theme", ["--db", db, "theme", "--repo", repo, "--weeks", str(weeks)])


@task(task_run_name="report")
def report(repo: str, week: str | None, db: str, no_llm: bool) -> None:
    argv = ["--db", db, "report", "--repo", repo]
    if week:
        argv += ["--week", week]
    if no_llm:
        argv.append("--no-llm")
    _run("report", argv)


@flow(name="weekly-support-trends")
def weekly_report(repo: str = cli.DEFAULT_REPO, weeks: int = 2, week: str | None = None,
                  db: str = str(cli.db.DEFAULT_DB), no_llm: bool = False) -> None:
    """Fetch the last `weeks` weeks, theme new issues, write the report for `week`
    (default: the last full week)."""
    log = get_run_logger()
    fetch(repo, weeks, db)
    theme(repo, weeks, db)
    report(repo, week, db, no_llm)
    log.info("Weekly report written for %s", week or "the last full week")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Run the weekly report as a Prefect flow")
    p.add_argument("--repo", default=cli.DEFAULT_REPO)
    p.add_argument("--weeks", type=int, default=2, help="weeks to fetch and theme (default 2)")
    p.add_argument("--week", help="report week, e.g. 2026-W45 (default: last full week)")
    p.add_argument("--no-llm", action="store_true", help="skip recommendations (no model calls)")
    p.add_argument("--serve", metavar="CRON", help='serve on a cron schedule, e.g. "15 7 * * 1"')
    args = p.parse_args(argv)
    params = {"repo": args.repo, "weeks": args.weeks, "week": args.week, "no_llm": args.no_llm}
    if args.serve:
        weekly_report.serve(name="weekly-support-trends", cron=args.serve, parameters=params)
    else:
        weekly_report(**params)


if __name__ == "__main__":
    main()
