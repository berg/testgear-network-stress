# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML rendering for a run, and for a comparison across backends.

Rendered from the same `Result` records the JSON report carries, so the two
cannot disagree about what happened.

Two rules shape the styling, and both come from the way this suite has already
been misread:

- **A skip is not a muted pass.** Skips get their own colour and their own
  count, because at a glance a skipped check reads like a passing one, and the
  ones that matter are exactly those that stay skipped run after run without
  anybody noticing.

- **Disagreement is what the eye should land on first.** In the comparison
  matrix a row where every backend agrees is quiet; a row where they differ is
  marked, because a check that fails on one implementation and passes on
  another is a much stronger claim than a failure alone.

Output is one self-contained file -- no external assets -- so it can go on an
upstream issue or open on a bench machine with no network.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

STYLE = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --rule: #e0e0e0;
  --panel: #f7f7f7; --pass: #1a7f37; --fail: #c1121f; --skip: #b45309;
  --pass-bg: #e8f5ec; --fail-bg: #fdeaec; --skip-bg: #fdf3e3;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181c; --fg: #e6e6e6; --muted: #9aa0a6; --rule: #2c3038;
    --panel: #1e2127; --pass: #4ade80; --fail: #f87171; --skip: #fbbf24;
    --pass-bg: #17301f; --fail-bg: #331a1c; --skip-bg: #322614;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 68rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .6rem; padding-bottom: .3rem;
     border-bottom: 1px solid var(--rule); }
.sub { color: var(--muted); margin: 0 0 1.5rem; }
.tally { display: flex; gap: .5rem; flex-wrap: wrap; margin: 0 0 1.5rem; }
.pill { padding: .3rem .7rem; border-radius: 999px; font-weight: 600;
        font-size: .85rem; }
.pill.pass { background: var(--pass-bg); color: var(--pass); }
.pill.fail { background: var(--fail-bg); color: var(--fail); }
.pill.skip { background: var(--skip-bg); color: var(--skip); }
.pill.plain { background: var(--panel); color: var(--muted); }
.env { background: var(--panel); border: 1px solid var(--rule); border-radius: 8px;
       padding: .8rem 1rem; margin: 0 0 1.5rem; font-family: var(--mono);
       font-size: .82rem; overflow-x: auto; }
.env div { display: flex; gap: .6rem; }
.env dt { color: var(--muted); min-width: 12rem; }
.warn { color: var(--skip); }
.check { border-left: 3px solid var(--rule); padding: .45rem .8rem;
         margin: .3rem 0; border-radius: 0 6px 6px 0; }
.check.FAIL { border-left-color: var(--fail); background: var(--fail-bg); }
.check.SKIP { border-left-color: var(--skip); background: var(--skip-bg); }
.check.PASS { border-left-color: var(--pass); }
.name { font-weight: 500; }
.tag { font-family: var(--mono); font-size: .72rem; font-weight: 700;
       letter-spacing: .04em; margin-right: .6rem; }
.tag.PASS { color: var(--pass); } .tag.FAIL { color: var(--fail); }
.tag.SKIP { color: var(--skip); }
.detail { font-family: var(--mono); font-size: .8rem; color: var(--muted);
          white-space: pre-wrap; margin-top: .3rem; }
.rule { font-size: .75rem; color: var(--muted); margin-left: .5rem; }
details > summary { cursor: pointer; color: var(--muted); font-size: .9rem;
                    padding: .3rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--rule); }
th { font-weight: 600; color: var(--muted); font-size: .8rem; }
td.cell { font-family: var(--mono); font-weight: 700; font-size: .78rem; }
td.cell.PASS { color: var(--pass); } td.cell.FAIL { color: var(--fail); }
td.cell.SKIP { color: var(--skip); } td.cell.none { color: var(--muted); }
tr.differs { background: var(--fail-bg); }
tr.differs td:first-child::before { content: "\\25C6\\00a0"; color: var(--fail); }
.scroll { overflow-x: auto; }
footer { margin-top: 3rem; color: var(--muted); font-size: .8rem; }
"""


def _esc(value) -> str:
    return html.escape(str(value))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{STYLE}</style>\n"
        f"</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n"
    )


def _env_block(context: dict) -> str:
    rows = []
    for key, value in context.items():
        cls = ' class="warn"' if key == "warning" else ""
        rows.append(
            f"<div{cls}><dt>{_esc(key)}</dt><dd>{_esc(value)}</dd></div>"
        )
    return f'<div class="env">{"".join(rows)}</div>'


def _tally(passed: int, failed: int, skipped: int, extra: str = "") -> str:
    pills = [f'<span class="pill pass">{passed} passed</span>']
    if failed:
        pills.append(f'<span class="pill fail">{failed} failed</span>')
    if skipped:
        pills.append(f'<span class="pill skip">{skipped} skipped</span>')
    if extra:
        pills.append(f'<span class="pill plain">{_esc(extra)}</span>')
    return f'<div class="tally">{"".join(pills)}</div>'


def render_run(report: dict) -> str:
    """One run: the provenance, the tally, then every check in source order."""
    script = report.get("script", "run")
    context = report.get("context", {})
    results = report.get("results", [])

    failures = [r for r in results if r["outcome"] == FAIL]
    skips = [r for r in results if r["outcome"] == SKIP]
    passes = [r for r in results if r["outcome"] == PASS]

    def block(result: dict) -> str:
        outcome = result["outcome"]
        rule = (
            f'<span class="rule">{_esc(result["rule"])}</span>'
            if result.get("rule")
            else ""
        )
        detail = (
            f'<div class="detail">{_esc(result["detail"])}</div>'
            if result.get("detail") and result["detail"] != result["name"]
            else ""
        )
        return (
            f'<div class="check {outcome}">'
            f'<span class="tag {outcome}">{outcome}</span>'
            f'<span class="name">{_esc(result["name"])}</span>{rule}'
            f"{detail}</div>"
        )

    parts = [
        f"<h1>{_esc(script)}</h1>",
        f'<p class="sub">{_esc(report.get("elapsed", 0))}s</p>',
        _tally(len(passes), len(failures), len(skips)),
        _env_block(context),
    ]

    # Failures and skips first and open; passes folded away. A report is read
    # to find out what went wrong, and 40 green lines above the one red one is
    # a report that buries its own point.
    if failures:
        parts.append("<h2>Failures</h2>")
        parts.extend(block(r) for r in failures)
    if skips:
        parts.append("<h2>Skipped &mdash; not passes</h2>")
        parts.extend(block(r) for r in skips)
    if passes:
        parts.append(
            f"<h2>Passed</h2><details><summary>{len(passes)} checks</summary>"
            + "".join(block(r) for r in passes)
            + "</details>"
        )
    if report.get("notes"):
        parts.append(
            "<h2>Notes</h2><details><summary>"
            f'{len(report["notes"])} notes</summary>'
            + "".join(f'<div class="detail">{_esc(n)}</div>' for n in report["notes"])
            + "</details>"
        )
    return _page(script, "\n".join(parts))


def render_matrix(runs: list[dict], title: str = "Backend comparison") -> str:
    """Several runs side by side: checks down, backends across.

    A run that could not happen at all still gets a column, carrying the reason
    -- a comparison with a column silently missing reads like agreement between
    whatever is left.
    """
    columns = [r.get("label", r.get("context", {}).get("backend", "?")) for r in runs]

    # Union of check names, keeping the order of the first run that has each,
    # so the table reads in the order the checks were written.
    order: list[str] = []
    display: dict[str, str] = {}
    seen = set()
    for run in runs:
        for result in run.get("results", []):
            key = result.get("key", result["name"])
            if key not in seen:
                seen.add(key)
                order.append(key)
                display[key] = result["name"]

    by_run = [
        {result.get("key", result["name"]): result for result in run.get("results", [])}
        for run in runs
    ]

    rows = []
    differing = 0
    for key in order:
        name = display[key]
        cells = []
        outcomes = set()
        rule = ""
        for lookup in by_run:
            result = lookup.get(key)
            if result is None:
                cells.append('<td class="cell none">&mdash;</td>')
                outcomes.add(None)
                continue
            rule = rule or result.get("rule", "")
            outcomes.add(result["outcome"])
            cells.append(f'<td class="cell {result["outcome"]}">{result["outcome"]}</td>')

        differs = len(outcomes) > 1
        differing += differs
        rule_html = f'<span class="rule">{_esc(rule)}</span>' if rule else ""
        rows.append(
            f'<tr class="{"differs" if differs else ""}">'
            f"<td>{_esc(name)}{rule_html}</td>{''.join(cells)}</tr>"
        )

    header = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    env_blocks = "".join(
        f"<h2>{_esc(label)}</h2>" + _env_block(run.get("context", {}))
        for label, run in zip(columns, runs)
    )

    body = (
        f"<h1>{_esc(title)}</h1>"
        f'<p class="sub">{len(order)} checks across {len(runs)} '
        f"{'backend' if len(runs) == 1 else 'backends'}. "
        f"{differing} disagree.</p>"
        + _tally(
            sum(
                1
                for k in order
                if all(l.get(k, {}).get("outcome") == PASS for l in by_run)
            ),
            differing,
            0,
            extra=f"{len(order)} checks",
        )
        + '<div class="scroll"><table><thead><tr><th>Check</th>'
        + header
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + env_blocks
        + "<footer>Rows marked &#9670; are where the implementations disagree. "
        "A check that fails on one and passes on another is a disparity, which "
        "is a stronger claim than a failure.</footer>"
    )
    return _page(title, body)


def write_run(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(render_run(report), encoding="utf-8")
    return path


def write_matrix(runs: list[dict], path: str | Path, title: str = "Backend comparison") -> Path:
    path = Path(path)
    path.write_text(render_matrix(runs, title), encoding="utf-8")
    return path


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
