"""Render the weekly report as one static HTML file: no JavaScript, no external files.

The chart is small multiples: one row per theme, 8 weekly bars on a shared scale, so rows
can be compared directly. One hue throughout; the report week is the strong step, the
earlier weeks a light step. Rising themes carry a text badge (not colour alone), and
every bar has a native tooltip (SVG <title>). The tables below the chart are the
accessible text view of the same numbers.
"""

from __future__ import annotations

from datetime import timedelta
from html import escape

from .recommend import KINDS, ThemeRecommendation
from .report import plural
from .themes import ThemeSet
from .trends import HISTORY_WEEKS, RISING_FACTOR, RISING_MIN_ISSUES, WeekTrends, parse_week

# Chart geometry (SVG user units).
BAR_W, BAR_GAP, STRIP_H = 22, 6, 36


CSS = """
:root {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #6f6d68;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --bar-target: #2a78d6; --bar-hist: #86b6ef; --link: #1c5fae;
  --badge-bg: #e6effb; --badge-ink: #123e73;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #9a988f;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --bar-target: #3987e5; --bar-hist: #184f95; --link: #7fb2f0;
    --badge-bg: #16325a; --badge-ink: #dce9fa;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 880px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 32px 0 8px; }
h3 { font-size: 1rem; margin: 20px 0 6px; }
p, li { color: var(--ink); }
.sub, .note, figcaption, footer { color: var(--ink-2); }
.note { font-size: 0.9rem; }
a { color: var(--link); }
figure { margin: 0; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; }
.warn { border-left: 3px solid var(--axis); padding-left: 10px; color: var(--ink-2); }
.badge { display: inline-block; font-size: 0.75rem; font-weight: 600; padding: 0 6px;
  border-radius: 4px; background: var(--badge-bg); color: var(--badge-ink);
  margin-left: 6px; vertical-align: 1px; }
.kind { font-weight: 600; }
.chart { display: grid; grid-template-columns: minmax(0, 1fr) auto 3ch; gap: 2px 12px;
  align-items: center; }
.chart .name { font-size: 0.9rem; overflow-wrap: anywhere; }
.chart .val { font-variant-numeric: tabular-nums; font-weight: 600; text-align: right; }
.chart svg { display: block; width: 224px; height: auto; }
.chart .head { color: var(--muted); font-size: 0.75rem; }
.bar-target { fill: var(--bar-target); }
.bar-hist { fill: var(--bar-hist); }
.base { stroke: var(--axis); stroke-width: 1; }
.hit { fill: transparent; }
.hit:hover + path, g:hover > path { opacity: 0.75; }
.wk { fill: var(--muted); font-size: 10px; }
.wk.now { fill: var(--ink); font-weight: 700; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--grid); text-align: left; }
th { color: var(--ink-2); font-weight: 600; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { font-weight: 700; border-bottom: none; }
@media (max-width: 560px) {
  .chart { grid-template-columns: minmax(0, 1fr) 3ch; }
  .chart .name { grid-column: 1 / -1; margin-top: 6px; }
  .chart .name.head { display: none; }
  .chart svg { width: 100%; }
}
"""


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """A bar with rounded top corners, flat on the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h:.1f} Z")


def strip_svg(name: str, weeks: list[str], counts: list[int], scale_max: int) -> str:
    width = len(weeks) * (BAR_W + BAR_GAP) - BAR_GAP
    parts = [f'<svg viewBox="0 0 {width} {STRIP_H + 1}" role="img" '
             f'aria-label="{escape(name)}: {", ".join(map(str, counts))} issues per week">']
    for i, (week, n) in enumerate(zip(weeks, counts, strict=True)):
        x = i * (BAR_W + BAR_GAP)
        h = STRIP_H * n / scale_max if scale_max else 0
        cls = "bar-target" if i == len(weeks) - 1 else "bar-hist"
        tip = escape(f"{name}, {week}: {plural(n, 'issue')}")
        parts.append(f'<g><title>{tip}</title>'
                     f'<rect class="hit" x="{x}" y="0" width="{BAR_W}" height="{STRIP_H}"/>')
        if n:
            parts.append(f'<path class="{cls}" d="{_bar_path(x, STRIP_H - h, BAR_W, h)}"/>')
        parts.append("</g>")
    parts.append(f'<line class="base" x1="0" y1="{STRIP_H + 0.5}" x2="{width}" '
                 f'y2="{STRIP_H + 0.5}"/></svg>')
    return "".join(parts)


def week_axis_svg(weeks: list[str]) -> str:
    width = len(weeks) * (BAR_W + BAR_GAP) - BAR_GAP
    labels = []
    for i, w in enumerate(weeks):
        x = i * (BAR_W + BAR_GAP) + BAR_W / 2
        cls = "wk now" if i == len(weeks) - 1 else "wk"
        labels.append(f'<text class="{cls}" x="{x}" y="11" text-anchor="middle">'
                      f'{escape(w.split("-")[1])}</text>')
    return f'<svg viewBox="0 0 {width} 14" aria-hidden="true">{"".join(labels)}</svg>'


def chart_html(wt: WeekTrends, names: dict[str, str], chart_weeks: list[str],
               chart: dict[str, list[int]], empty_weeks: list[str] | None = None,
               unthemed_weeks: dict[str, int] | None = None) -> str:
    scale_max = max((n for counts in chart.values() for n in counts), default=0)
    rising = {t.theme for t in wt.rising()}
    rows = ['<div class="name head">Theme</div>', week_axis_svg(chart_weeks),
            f'<div class="val head">{escape(chart_weeks[-1].split("-")[1])}</div>']
    for t in wt.trends:  # same order as the table: busiest this week first
        name = names.get(t.theme, t.theme)
        badge = '<span class="badge">Rising</span>' if t.theme in rising else ""
        rows += [f'<div class="name">{escape(name)}{badge}</div>',
                 strip_svg(name, chart_weeks, chart[t.theme], scale_max),
                 f'<div class="val">{chart[t.theme][-1]}</div>']
    return (f'<figure class="card"><div class="chart">{"".join(rows)}</div>'
            f'<figcaption class="note">Issues opened per ISO week, '
            f'{escape(chart_weeks[0])} to {escape(chart_weeks[-1])}. All rows share one '
            f'scale (tallest bar = {scale_max}). The last, stronger blue bar is the report '
            'week. Hover a bar for its number; the tables below have every value.'
            + (f' No issues are stored for {escape(", ".join(empty_weeks))}, so '
               'those bars are empty, not zero.' if empty_weeks else '')
            + (' Some weeks have issues without a theme, so their bars are too low: '
               + escape(", ".join(f"{w} ({n})" for w, n in unthemed_weeks.items()))
               + '. Run trends theme and regenerate.' if unthemed_weeks else '')
            + '</figcaption></figure>')


def render(repo: str, wt: WeekTrends, ts: ThemeSet, recs: list[ThemeRecommendation] | None,
           chart_weeks: list[str], chart: dict[str, list[int]], cost_line: str = "",
           empty_weeks: list[str] | None = None,
           unthemed_weeks: dict[str, int] | None = None) -> str:
    names = {t.id: t.name for t in ts.themes}
    monday = parse_week(wt.week)
    sunday = monday + timedelta(days=6)
    rising = wt.rising()
    e = escape
    out = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Issue trends {e(wt.week)}</title>",
        '<meta name="color-scheme" content="light dark">',
        f"<style>{CSS}</style></head><body><main>",
        f"<h1>What's breaking: {e(repo)}, {e(wt.week)}</h1>",
        f'<p class="sub">{monday:%d %b} - {sunday:%d %b %Y}. {wt.total_issues} issues '
        f"opened; {wt.total_issues - wt.unthemed} themed against themes.yaml version "
        f"{wt.themes_version}.</p>",
    ]
    if wt.unthemed:
        out.append(f'<p class="warn">{wt.unthemed} issues this week have no theme yet, so '
                   "counts are low. Run <code>trends theme</code> and regenerate.</p>")
    if wt.missing_history:
        out.append(f'<p class="warn">No issues stored for {e(", ".join(wt.missing_history))};'
                   " the averages treat those weeks as zero.</p>")
    if wt.unthemed_history:
        weeks = ", ".join(f"{w} ({n})" for w, n in wt.unthemed_history.items())
        out.append(f'<p class="warn">Issues without a theme in earlier weeks: {e(weeks)}. The '
                   "averages count themed issues only, so they are too low. Run "
                   "<code>trends theme</code> and regenerate.</p>")

    out.append("<h2>Rising themes</h2>")
    if rising:
        out.append("<ul>" + "".join(
            f"<li><strong>{e(names.get(t.theme, t.theme))}</strong>: {t.count} issues, "
            f"{HISTORY_WEEKS}-week average {t.average:.1f} "
            f"({f'{t.ratio:.1f}x' if t.ratio is not None else 'new'})</li>"
            for t in rising) + "</ul>")
    else:
        out.append("<p>No theme is rising this week.</p>")
    out.append(f'<p class="note">Rising means at least {RISING_MIN_ISSUES} issues this week '
               f"and at least {RISING_FACTOR:g}x the average of the previous {HISTORY_WEEKS} "
               "weeks.</p>")

    out.append("<h2>Recommendations</h2>")
    if recs is None:
        out.append("<p>Skipped (<code>--no-llm</code>).</p>")
    elif not recs:
        out.append("<p>None: no rising themes (other than <code>other</code>).</p>")
    for rec in recs or []:
        out.append(f"<h3>{e(names.get(rec.theme, rec.theme))}</h3><ul>")
        for a in rec.actions:
            cites = ", ".join(
                f'<a href="{e(u)}">#{e(u.rstrip("/").rsplit("/", 1)[-1])}</a>'
                for u in a.citations)
            out.append(f'<li><span class="kind">{e(KINDS[a.kind])}:</span> '
                       f"<strong>{e(a.title)}</strong><br>{e(a.detail)}<br>"
                       f'<span class="note">Based on: {cites}</span></li>')
        if not rec.actions:
            out.append("<li>No recommendation passed the citation check.</li>")
        out.append("</ul>")
        if rec.problems:
            out.append(f'<p class="note">Checks: {len(rec.problems)} problem(s) fixed or '
                       f"dropped: {e('; '.join(rec.problems[:4]))}</p>")

    out += ["<h2>Theme counts over 8 weeks</h2>",
            chart_html(wt, names, chart_weeks, chart, empty_weeks, unthemed_weeks)]

    out += ["<h2>All themes</h2>", '<div class="table-wrap"><table><thead><tr>',
            f'<th>Theme</th><th class="n">{e(wt.week)}</th><th class="n">Previous week</th>'
            f'<th class="n">Change</th><th class="n">{HISTORY_WEEKS}-week avg</th>'
            '<th class="n">vs avg</th><th>Rising</th></tr></thead><tbody>']
    for t in wt.trends:
        ratio = (f"{t.ratio:.1f}x" if t.ratio is not None else ("new" if t.count else "-"))
        change = f"+{t.change}" if t.change > 0 else str(t.change)
        out.append(f"<tr><td>{e(names.get(t.theme, t.theme))}</td><td class=n>{t.count}</td>"
                   f"<td class=n>{t.previous}</td><td class=n>{change}</td>"
                   f"<td class=n>{t.average:.1f}</td><td class=n>{ratio}</td>"
                   f"<td>{'yes' if t.rising else ''}</td></tr>")
    out.append(f'<tr class="total"><td>Total</td><td class=n>{sum(t.count for t in wt.trends)}'
               f"</td><td class=n>{sum(t.previous for t in wt.trends)}</td>"
               "<td></td><td></td><td></td><td></td></tr></tbody></table></div>")

    out.append("<h2>Most-reacted issues per theme</h2>")
    for t in wt.trends:
        if not t.top_issues:
            continue
        out.append(f"<h3>{e(names.get(t.theme, t.theme))}</h3><ul>")
        for i in t.top_issues:
            out.append(f'<li><a href="{e(i.url)}">#{i.number}</a> {e(i.title)} '
                       f'<span class="note">({plural(i.reactions, "reaction")}, '
                       f'{plural(i.comments, "comment")}, {e(i.state)})</span></li>')
        out.append("</ul>")

    out += ["<h2>Weekly counts</h2>", '<div class="table-wrap"><table><thead><tr><th>Theme</th>']
    out += [f'<th class="n">{e(w)}</th>' for w in chart_weeks]
    out.append("</tr></thead><tbody>")
    for t in wt.trends:
        out.append(f"<tr><td>{e(names.get(t.theme, t.theme))}</td>"
                   + "".join(f"<td class=n>{n}</td>" for n in chart[t.theme]) + "</tr>")
    out.append("</tbody></table></div>")

    out.append(f'<footer class="note"><p>Generated by support-trends-report. Themes: version '
               f"{ts.version} ({len(ts.themes)} themes). Issue weeks are ISO weeks by creation "
               f"date (UTC).{(' ' + e(cost_line)) if cost_line else ''}</p></footer>")
    out.append("</main></body></html>")
    return "\n".join(out) + "\n"
