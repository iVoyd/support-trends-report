"""Command line for support-trends-report.

Commands: fetch, seed-themes, theme, status, report, check-report, compare-themes,
stability, import-triage. Run `trends <command> --help` for options.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import (
    classify,
    compare,
    db,
    github,
    llm,
    recommend,
    report,
    report_html,
    stability,
    themes,
    trends,
    verify,
)
from .redact import redact_emails
from .triage_import import import_triage

DEFAULT_REPO = "supabase/supabase"


def iso_week(ts: str) -> str:
    y, w, _ = datetime.strptime(ts[:10], "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def cmd_fetch(args, conn) -> int:
    since = github.window_start(args.weeks)
    print(f"Fetching {args.repo} issues created since {since} (search API, one query per "
          "week) ...")
    res = github.fetch_issues(conn, args.repo, since, http_get=github.urllib_get,
                              refresh=args.refresh,
                              record_dir=Path(args.record_dir) if args.record_dir else None)
    print(f"weeks read: {res.weeks_read}, weeks already done: {res.weeks_skipped}, "
          f"requests: {res.requests}, issues stored: {res.issues}"
          + (f", busy ranges split: {res.splits}" if res.splits else ""))
    for w in res.warnings:
        print(f"  warning: {w}")
    return 0


def cmd_seed_themes(args, conn) -> int:
    path = Path(args.themes)
    version = 1
    if path.exists():
        if not args.force:
            print(f"{path} exists and is frozen. Use --force to reseed (bumps the version).")
            return 1
        version = themes.load(path).version + 1
    since = github.window_start(args.weeks)
    client = llm.AnthropicJSON(model=args.model)
    run_id = llm.start_run(conn, "seed-themes", client.model)
    usage = llm.Usage()
    try:
        ts, usage = themes.seed(client, conn, args.repo, since, args.sample, version)
    finally:
        cost = llm.finish_run(conn, run_id, client.model, usage)
    themes.dump(ts, path)
    print(f"Wrote {path} (version {ts.version}, {len(ts.themes)} themes). Cost ${cost:.4f}.")
    print("Review it, edit if needed, and commit it before running `trends theme`.")
    for t in ts.themes:
        print(f"  {t.id:24} {t.name}")
    return 0


def stored_window(args) -> str:
    """--weeks N limits to the last N weeks; without it, every stored issue counts."""
    return github.window_start(args.weeks) if args.weeks else ""


def cmd_theme(args, conn) -> int:
    ts = themes.load(Path(args.themes))
    since = stored_window(args)
    todo = classify.unthemed(conn, args.repo, since, ts.version)
    if args.limit is not None:
        todo = todo[: args.limit]
    if args.estimate:
        sys_tokens = len(classify.system_prompt(ts)) // 4
        user_tokens = sum(len(classify.user_prompt([r])) for r in todo) // 4
        batches = -(-len(todo) // args.batch_size)
        est = llm.Usage(input_tokens=user_tokens, output_tokens=25 * len(todo),
                        cache_write_tokens=sys_tokens,
                        cache_read_tokens=sys_tokens * max(batches - 1, 0))
        print(f"{len(todo)} issues to theme in {batches} batches; rough estimate "
              f"${est.cost(args.model):.4f} with {args.model} (chars/4 token guess).")
        return 0
    if not todo:
        print("Nothing to theme: every issue in the window has a theme for "
              f"themes.yaml version {ts.version}.")
        return 0
    client = llm.AnthropicJSON(model=args.model, effort=args.effort,
                               thinking=args.thinking)
    run_id = llm.start_run(conn, "theme", client.model)
    res = classify.ThemeRunResult()
    try:
        res = classify.theme_issues(client, conn, ts, args.repo, since,
                                    batch_size=args.batch_size, run_id=run_id,
                                    limit=args.limit)
    finally:
        cost = llm.finish_run(conn, run_id, client.model, res.usage)
    u = res.usage
    print(f"themed: {res.themed}, already done: {res.skipped_already_done}, "
          f"failed: {len(res.failed)}")
    print(f"calls: {u.calls}, input: {u.input_tokens}, cache write: {u.cache_write_tokens}, "
          f"cache read: {u.cache_read_tokens}, output: {u.output_tokens}, cost: ${cost:.4f}")
    for p in res.problems[:20]:
        print(f"  note: {p}")
    if res.failed:
        print(f"  unthemed after retry: {res.failed[:20]} (rerun `trends theme` to retry)")
    return 0 if not res.failed else 2


def cmd_status(args, conn) -> int:
    since = stored_window(args)
    path = Path(args.themes)
    version = themes.load(path).version if path.exists() else None
    rows = conn.execute(
        "SELECT i.created_at, t.theme FROM issues i LEFT JOIN issue_themes t "
        "ON t.repo = i.repo AND t.number = i.number AND t.themes_version = ? "
        "WHERE i.repo = ? AND i.created_at >= ?",
        (version, args.repo, since),
    ).fetchall()
    total, themed = Counter(), Counter()
    for r in rows:
        week = iso_week(r["created_at"])
        total[week] += 1
        themed[week] += r["theme"] is not None
    window = f"since {since}" if since else "all stored issues"
    print(f"{args.repo}, {window}, themes.yaml version {version}")
    print(f"{'week':10} {'issues':>7} {'themed':>7}")
    for week in sorted(total):
        print(f"{week:10} {total[week]:7} {themed[week]:7}")
    print(f"{'total':10} {sum(total.values()):7} {sum(themed.values()):7}")
    runs = conn.execute(
        "SELECT command, COUNT(*) n, SUM(calls) calls, SUM(cost_usd) cost FROM llm_runs "
        "GROUP BY command ORDER BY command"
    ).fetchall()
    for r in runs:
        print(f"LLM {r['command']}: {r['n']} runs, {r['calls']} calls, ${r['cost'] or 0:.4f}")
    return 0


def cmd_report(args, conn) -> int:
    ts = themes.load(Path(args.themes))
    week = args.week or trends.last_full_week()
    trends.parse_week(week)  # clear error for a malformed --week
    wt = trends.week_trends(conn, args.repo, ts.ids, ts.version, week)
    if wt.total_issues == 0:
        print(f"No issues stored for {week}. Run `trends fetch` (and `trends theme`) first.")
        return 1
    recs, cost_line = None, ""
    if not args.no_llm:
        recs, cost_line = recommendations_for(args, conn, ts, wt)
    chart_weeks, chart = trends.chart_counts(conn, args.repo, ts.version, ts.ids, week)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out_dir / f"{week}.md", out_dir / f"{week}.html"
    md = report.render(args.repo, wt, ts, recs, cost_line)
    md_path.write_text(redact_emails(md))
    unthemed_chart = trends.unthemed_by_week(conn, args.repo, ts.version, chart_weeks)
    html_path.write_text(redact_emails(report_html.render(
        args.repo, wt, ts, recs, chart_weeks, chart, cost_line,
        empty_weeks=trends.empty_weeks(conn, args.repo, chart_weeks),
        unthemed_weeks=unthemed_chart)))
    print(f"Wrote {md_path} and {html_path}: {wt.total_issues} issues, "
          f"{len(wt.rising())} rising themes.")
    if unthemed_chart:
        weeks = ", ".join(f"{w} ({n})" for w, n in unthemed_chart.items())
        print(f"  warning: issues without a theme in {weeks}; counts for those weeks are "
              "too low. Run `trends theme`, then this report again (saved recommendations "
              "are reused).")
    if cost_line:
        print(cost_line)
    problems = verify.check_against_db(md, wt, ts)
    for p in problems:
        print(f"  check failed: {p}")
    return 0 if not problems else 3


def recommendations_for(args, conn, ts, wt) -> tuple[list, str]:
    """Saved recommendations for this week where present; the model only for the rest."""
    rising = [t for t in wt.rising() if t.theme != themes.OTHER][:recommend.MAX_THEMES]
    saved = {} if args.regenerate else {
        t.theme: recommend.load(conn, args.repo, wt.week, ts.version, t.theme) for t in rising}
    missing = [t for t in rising if saved.get(t.theme) is None]
    if not missing:
        note = "Recommendations reused from an earlier run (no model calls)." if rising else ""
        return [saved[t.theme] for t in rising], note
    client = llm.AnthropicJSON(model=args.model)
    run_id = llm.start_run(conn, "report", client.model)
    usage = llm.Usage()
    try:
        by_id = {t.id: t for t in ts.themes}
        for t in missing:
            issues = trends.top_issues(conn, args.repo, ts.version, t.theme, wt.week, limit=None)
            rec, u = recommend.recommend_theme(client, by_id[t.theme], t, issues)
            usage += u
            recommend.save(conn, args.repo, wt.week, ts.version, rec, client.model, run_id)
            saved[t.theme] = rec
    finally:
        cost = llm.finish_run(conn, run_id, client.model, usage)
    reused = len(rising) - len(missing)
    cost_line = (f"Recommendations: {usage.calls} calls to {client.model}, "
                 f"{usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens}"
                 f" input / {usage.output_tokens} output tokens, ${cost:.4f}"
                 + (f"; {reused} theme(s) reused from an earlier run." if reused else "."))
    return [saved[t.theme] for t in rising], cost_line


def cmd_check_report(args, conn) -> int:
    path = Path(args.report)
    week = path.stem
    ts = themes.load(Path(args.themes))
    md = path.read_text()
    if args.internal_only:
        problems = verify.check_internal(md)
    else:
        trends.parse_week(week)
        wt = trends.week_trends(conn, args.repo, ts.ids, ts.version, week)
        problems = verify.check_against_db(md, wt, ts)
    if problems:
        print(f"{path}: {len(problems)} problem(s)")
        for p in problems:
            print(f"  {p}")
        return 3
    where = "internally consistent" if args.internal_only else "matches the database"
    print(f"{path}: every number {where}.")
    return 0


def cmd_compare_themes(args, conn) -> int:
    ts = themes.load(Path(args.themes))
    new = args.to_version or ts.version
    old = args.from_version or new - 1
    if args.week:
        trends.parse_week(args.week)
    c = compare.compare(conn, args.repo, old, new, args.week)
    if c.compared == 0 and c.only_old == 0:
        print(f"No issues are themed under v{old}.")
        return 1
    scope = args.week or "all stored issues"
    print(compare.render(c, {t.id: t.name for t in ts.themes}, scope, args.list))
    return 0


def cmd_stability(args, conn) -> int:
    ts = themes.load(Path(args.themes))
    client = llm.AnthropicJSON(model=args.model, effort=args.effort, thinking=args.thinking)
    run_id = llm.start_run(conn, "stability", client.model)
    res = None
    try:
        res = stability.measure(client, conn, ts, args.repo, args.sample, args.passes,
                                args.batch_size, args.shuffle)
    finally:
        cost = llm.finish_run(conn, run_id, client.model,
                              res.usage if res else llm.Usage())
    settings = (f"{client.model}, themes v{ts.version}, effort {args.effort}, thinking "
                f"{args.thinking}, batches of {args.batch_size}"
                + (", shuffled between passes" if args.shuffle else ", same batches"))
    print(stability.render(res, {t.id: t.name for t in ts.themes}, settings, args.list))
    print(f"Cost: ${cost:.4f} ({res.usage.calls} calls). Nothing was saved to issue_themes.")
    return 0


def cmd_import_triage(args, conn) -> int:
    n = import_triage(conn, Path(args.source), args.repo)
    print(f"imported {n} triage records from {args.source}")
    return 0


def add_model_settings(sp) -> None:
    sp.add_argument("--effort", default="low", choices=llm.EFFORTS,
                    help="output_config.effort (default %(default)s)")
    sp.add_argument("--thinking", default="disabled", choices=("disabled", "adaptive"),
                    help="thinking mode (default %(default)s; disabled needs effort <= high)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trends", description=__doc__)
    p.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite path (default %(default)s)")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, weeks=True, weeks_default=8):
        sp.add_argument("--repo", default=DEFAULT_REPO)
        if weeks:
            sp.add_argument("--weeks", type=int, default=weeks_default,
                            help="last N ISO weeks" + ("" if weeks_default else
                                                       " (default: every stored issue)"))
        return sp

    f = common(sub.add_parser("fetch", help="fetch issues into the SQLite cache (resumable)"))
    f.add_argument("--record-dir", help="also save raw API pages here (for test fixtures)")
    f.add_argument("--refresh", action="store_true",
                   help="re-read finished weeks too (updates state, comments, reactions)")
    f.set_defaults(func=cmd_fetch)

    s = common(sub.add_parser("seed-themes", help="propose themes.yaml from labels + a sample"))
    s.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    s.add_argument("--model", default=llm.DEFAULT_MODEL)
    s.add_argument("--sample", type=int, default=80)
    s.add_argument("--force", action="store_true", help="overwrite and bump the version")
    s.set_defaults(func=cmd_seed_themes)

    t = common(sub.add_parser("theme", help="assign one theme per issue"), weeks_default=None)
    t.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    t.add_argument("--model", default=llm.DEFAULT_MODEL)
    t.add_argument("--batch-size", type=int, default=20)
    t.add_argument("--limit", type=int, help="theme at most N issues (for a cheap trial)")
    t.add_argument("--estimate", action="store_true", help="print a cost estimate and stop")
    add_model_settings(t)
    t.set_defaults(func=cmd_theme)

    st = common(sub.add_parser("status", help="issues and themed counts per week, LLM cost"),
                weeks_default=None)
    st.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    st.set_defaults(func=cmd_status)

    r = common(sub.add_parser("report", help="write reports/<week>.md"), weeks=False)
    r.add_argument("--week", help="ISO week like 2026-W44 (default: last full week)")
    r.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    r.add_argument("--model", default=llm.DEFAULT_MODEL)
    r.add_argument("--out-dir", default="reports")
    r.add_argument("--no-llm", action="store_true", help="numbers only, no recommendations")
    r.add_argument("--regenerate", action="store_true",
                   help="ask the model again even if this week has saved recommendations")
    r.set_defaults(func=cmd_report)

    c = common(sub.add_parser("check-report",
                              help="check a report's numbers against the database"),
               weeks=False)
    c.add_argument("report", help="path like reports/2026-W44.md")
    c.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    c.add_argument("--internal-only", action="store_true",
                   help="only check the report against itself (no database needed)")
    c.set_defaults(func=cmd_check_report)

    cmp = common(sub.add_parser("compare-themes",
                                help="compare theme assignments between two versions"),
                 weeks=False)
    cmp.add_argument("--from", dest="from_version", type=int,
                     help="old version (default: current - 1)")
    cmp.add_argument("--to", dest="to_version", type=int,
                     help="new version (default: current themes.yaml)")
    cmp.add_argument("--week", help="limit to one ISO week, like 2026-W36")
    cmp.add_argument("--list", action="store_true", help="also list every moved issue")
    cmp.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    cmp.set_defaults(func=cmd_compare_themes)

    sb = common(sub.add_parser("stability",
                               help="theme a sample twice without saving; report agreement"),
                weeks=False)
    sb.add_argument("--themes", default=str(themes.DEFAULT_PATH))
    sb.add_argument("--model", default=llm.DEFAULT_MODEL)
    sb.add_argument("--sample", type=int, help="number of stored issues (default: all)")
    sb.add_argument("--passes", type=int, default=2)
    sb.add_argument("--batch-size", type=int, default=20)
    sb.add_argument("--shuffle", action="store_true",
                    help="use different batches in later passes (measures batch-context effect)")
    sb.add_argument("--list", action="store_true", help="list every issue that changed theme")
    add_model_settings(sb)
    sb.set_defaults(func=cmd_stability)

    it = common(sub.add_parser("import-triage", help="import support-triage-assistant records"),
                weeks=False)
    it.add_argument("source", help="path to support-triage-assistant's SQLite file")
    it.set_defaults(func=cmd_import_triage)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        return args.func(args, conn)
    except (github.GitHubError, themes.ThemeError, llm.LLMError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
