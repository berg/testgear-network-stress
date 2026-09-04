#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the comparison as a shareable page, one section per transport.

Distinct from `testgear/report.py`, which writes a standalone file for local
viewing; this emits body-only HTML for publishing as an artifact.

Any row with a non-passing result expands on click to the full detail -- the
whole traceback, the whole skip reason, per implementation. A grid of
PASS/FAIL/SKIP without that is a page you have to go and read the JSON to
understand, and a truncated reason is worse than none: it produced a
VI_ERROR_RSRC_NFOUND under a check named after keepalive, which reads as
nonsense until the frame it came from is visible.

    tools/artifact_matrix.py --out page.html \\
        --reports hislip=reports-hislip vxi11=reports-vxi11

Which columns a directory contributes, in what order and under what label,
comes from an `index.json` beside the reports when there is one -- written by
whatever produced them, and listing every column that was *meant* to run. A
column named there with no report still appears, carrying the reason. The
alternative is what this used to do: name three backends in a constant, skip
any file that was not there, and publish a table whose missing column reads as
agreement between the ones that remain.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import aggregate, backends  # noqa: E402

#: Fallback column order when a directory has no index.json. Only used to make
#: a hand-run directory deterministic; `load` sorts by display label either
#: way, and deliberately does not put any one implementation first.
FALLBACK_ORDER = list(backends.BACKENDS)

HERE = Path(__file__).resolve().parent.parent

STYLE = """
:root{
  --ground:#F6F8F8; --panel:#FFFFFF; --ink:#0F1719; --muted:#5C6E72;
  --rule:#D9E2E3; --rule-soft:#EAF0F0;
  --pass:#17795A; --fail:#BE3A2B; --skip:#9C6A12; --accent:#0C7C86;
  --fail-wash:#FBF0EE; --skip-wash:#FDF6E8; --stripe:#BE3A2B;
  --sans:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --cond:"IBM Plex Sans Condensed","IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0D1315; --panel:#141D20; --ink:#DEE9EA; --muted:#8A9CA0;
  --rule:#243033; --rule-soft:#1A2427;
  --pass:#4FBF95; --fail:#F0796A; --skip:#DCA43A; --accent:#3FB6BF;
  --fail-wash:#201616; --skip-wash:#211B0F; --stripe:#F0796A;
}}
:root[data-theme="dark"]{
  --ground:#0D1315; --panel:#141D20; --ink:#DEE9EA; --muted:#8A9CA0;
  --rule:#243033; --rule-soft:#1A2427;
  --pass:#4FBF95; --fail:#F0796A; --skip:#DCA43A; --accent:#3FB6BF;
  --fail-wash:#201616; --skip-wash:#211B0F; --stripe:#F0796A;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased;
  overflow-x:hidden}
.wrap{max-width:76rem;margin:0 auto;padding:2.5rem 1.25rem 5rem;
  display:flex;flex-direction:column;gap:2.5rem}
.mast{display:flex;flex-direction:column;gap:.5rem}
.lede a{color:inherit;text-decoration-color:var(--accent);
  text-underline-offset:.18em}
.lede a:hover{color:var(--accent)}
h1{font-family:var(--cond);font-weight:700;font-size:clamp(1.7rem,4vw,2.5rem);
  margin:0;line-height:1.1;text-wrap:balance;letter-spacing:-.01em}
h2.proto{font-family:var(--cond);font-weight:700;font-size:1.4rem;margin:0;
  letter-spacing:-.005em}
.lede{margin:0;color:var(--muted);max-width:64ch}
.spec{font-family:var(--mono);font-size:.76rem;color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.35rem 1.4rem;padding-top:.65rem;
  border-top:1px solid var(--rule)}
.spec b{font-weight:500;color:var(--ink)}
.proto-block{display:flex;flex-direction:column;gap:1rem;
  padding-top:1.75rem;border-top:2px solid var(--rule)}
.cards{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:1rem 1.1rem;display:flex;flex-direction:column;gap:.7rem}
.card h3{font-family:var(--cond);font-size:1.05rem;font-weight:600;margin:0;
  letter-spacing:.01em}
.card .ver{font-family:var(--mono);font-size:.72rem;color:var(--muted);
  margin-top:-.45rem}
/* A column that produced nothing is drawn, dimmed and labelled. It is never
   dropped: the gap where a column should be reads as agreement between the
   implementations that remain, which is the opposite of what happened. */
.card.dead{border-style:dashed;opacity:.75}
.card.dead .ver{color:var(--skip)}
.card .ver.crashed{color:var(--fail)}
.card .ver.os{margin-top:0;letter-spacing:.04em;text-transform:uppercase}
th.st .os{display:block;font-family:var(--mono);font-weight:400;
  font-size:.62rem;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted)}
.counts{display:flex;gap:1.25rem;font-variant-numeric:tabular-nums}
.count{display:flex;flex-direction:column;line-height:1.15}
.count b{font-family:var(--cond);font-size:1.5rem;font-weight:700}
.count span{font-family:var(--cond);font-size:.68rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.count.p b{color:var(--pass)} .count.f b{color:var(--fail)} .count.s b{color:var(--skip)}
.bar{display:flex;height:5px;border-radius:2px;overflow:hidden;background:var(--rule-soft)}
.bar i{display:block}
.bar .p{background:var(--pass)} .bar .f{background:var(--fail)} .bar .s{background:var(--skip)}
h4{font-family:var(--cond);font-size:.8rem;font-weight:600;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);margin:0 0 .5rem}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);
  border-radius:3px}
table{border-collapse:collapse;width:100%;font-size:.83rem;min-width:46rem}
thead th{position:sticky;top:0;background:var(--panel);z-index:2;
  font-family:var(--cond);font-size:.7rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);text-align:left;font-weight:600;
  padding:.6rem .75rem;border-bottom:1px solid var(--rule)}
thead th.st{text-align:center;width:8rem}
td{padding:.4rem .75rem;border-bottom:1px solid var(--rule-soft);vertical-align:top}
tr.grp td{background:var(--rule-soft);font-family:var(--cond);font-weight:600;
  font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  padding:.5rem .75rem}
tr.differs{background:var(--fail-wash)}
tr.differs td:first-child{box-shadow:inset 3px 0 0 var(--stripe)}
/* The rows the whole exercise is for: pyvisa-py fails and every other
   implementation passes. A thicker stripe, because "disagrees with a shipping
   implementation of the same spec" is a much stronger claim than "disagrees
   with the others somehow". */
tr.only-one td:first-child{box-shadow:inset 5px 0 0 var(--fail)}
/* The check name links to the line that asserts it. Underlined only on hover:
   every row would otherwise be a wall of blue. */
a.src{color:var(--muted);text-decoration:none;font-family:var(--mono);
  font-size:.68rem;white-space:nowrap}
a.src:hover,a.src:focus{color:var(--accent);text-decoration:underline}
tr.grp a.src{opacity:1;color:inherit;font-family:inherit;font-size:inherit}
.grp-time{float:right;font-family:var(--mono);font-size:.68rem;
  color:var(--muted);font-weight:400}
tr.allskip{background:var(--skip-wash)}
.standouts{margin:.9rem 0 0}
details.standout{margin:0 0 .35rem;font-size:.85rem}
details.standout > summary{cursor:pointer;font-weight:600}
details.standout .lede{margin:.5rem 0 0;color:var(--muted);font-size:.82rem}
details.standout dl,details.standout dt{margin-top:.5rem;font-weight:600}
details.standout dd{margin:.15rem 0 0;color:var(--muted);
  font-family:var(--mono);font-size:.75rem;overflow-wrap:anywhere}
details.skipped-all{margin:.9rem 0 0;font-size:.85rem}
details.skipped-all summary{cursor:pointer;color:var(--muted)}
details.skipped-all dt{margin-top:.5rem;font-weight:600}
details.skipped-all dd{margin:.15rem 0 0;color:var(--muted);
  overflow-wrap:anywhere}
.check{max-width:34rem}
.rule{font-family:var(--mono);font-size:.68rem;color:var(--muted);display:block;
  margin-top:.1rem}
td.st{text-align:center;font-family:var(--mono);font-size:.7rem;font-weight:600;
  letter-spacing:.04em;white-space:nowrap}
td.PASS{color:var(--pass)} td.FAIL{color:var(--fail)}
td.SKIP{color:var(--skip)} td.none{color:var(--muted)}
/* A row with detail behind it announces itself with a caret and reacts to
   the pointer, so it is discoverable rather than something you have to guess
   is clickable. */
tr.expandable{cursor:pointer}
tr.expandable:hover td{background:var(--rule-soft)}
tr.expandable.differs:hover td{background:var(--fail-wash);filter:brightness(.97)}
.caret{display:inline-block;width:.85em;margin-right:.35em;color:var(--muted);
  transition:transform .12s ease}
.caret.spacer{visibility:hidden}
tr.expandable[aria-expanded="true"] .caret{transform:rotate(90deg)}
tr.detail-row td{padding:0 .75rem .9rem;background:var(--rule-soft)}
.detail-block{margin-top:.75rem;border-left:2px solid var(--rule);
  padding-left:.85rem}
.detail-head{font-size:.78rem;font-weight:600;
  letter-spacing:.02em;color:var(--muted);margin-bottom:.15rem}
.detail-head .tag{font-family:var(--mono);font-size:.68rem;font-weight:700;
  letter-spacing:.05em;margin-right:.45rem}
.detail-head .tag.PASS{color:var(--pass)}
.detail-head .tag.FAIL{color:var(--fail)}
.detail-head .tag.SKIP{color:var(--skip)}
.detail-sum{font-size:.82rem;margin-bottom:.4rem;max-width:80ch}
.detail-block pre{font-family:var(--mono);font-size:.72rem;line-height:1.5;
  margin:0;padding:.6rem .7rem;background:var(--panel);
  border:1px solid var(--rule);border-radius:3px;color:var(--ink);
  white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
code{font-family:var(--mono);font-size:.88em}
footer{color:var(--muted);font-size:.78rem;border-top:1px solid var(--rule);
  padding-top:1rem;max-width:70ch}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:38rem){
  /* The grid has to fit the screen. A min-width sized for a laptop turns
     every row into a sideways scroll on a phone, and the outcome columns are
     the part worth seeing without moving. */
  table{min-width:0;font-size:.78rem}
  td.check{white-space:normal;overflow-wrap:anywhere}
  th.st,td.st{padding-left:.35rem;padding-right:.35rem}
  .grp-time{float:none;display:block;margin-top:.15rem}
  .wrap{padding-left:.85rem;padding-right:.85rem}
  .detail-block pre{font-size:.68rem}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def esc(value) -> str:
    return html.escape(str(value))


def short_sha(commit: str) -> str:
    """A commit for a narrow slot: 12 hex characters, dirty marker kept.

    The full SHA stays in the title attribute, because the abbreviation is for
    reading and the full one is for pasting into `git show`.
    """
    commit = str(commit or "")
    head, _, rest = commit.partition("-")
    if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
        return f"{head[:12]}-{rest}" if rest else head[:12]
    return commit


def reason(result: dict) -> str:
    """The full text behind a non-passing result, kept verbatim.

    Nothing is trimmed. An earlier version cut tracebacks to their last line,
    which turned "the session could not be opened" into a bare
    VI_ERROR_RSRC_NFOUND under a check named after keepalive -- an error that
    reads as nonsense until you can see the frame it came from. The whole
    stack is what makes that diagnosable, so the whole stack is what the page
    shows.
    """
    return (result.get("detail") or "").strip()


def summary_line(detail: str) -> str:
    """One line for the collapsed state.

    For a traceback that is the exception, which is the last line; for an
    assertion message it is the message itself.
    """
    if not detail:
        return ""
    lines = [ln.strip() for ln in detail.splitlines() if ln.strip()]
    if not lines:
        return ""
    text = lines[-1] if "Traceback (most recent call last)" in detail else lines[0]
    return " ".join(text.split())


def load(root: Path) -> list[dict]:
    """Every column a directory contributes, in order, including dead ones.

    With an `index.json` the list of columns is whatever was *planned*, so a
    backend that was meant to run and did not still gets a column carrying its
    reason. Without one -- a hand-run directory of `<backend>.json` -- fall
    back to whatever is there, ordered by the backend table.
    """
    index = root / "index.json"
    if index.exists():
        planned = json.loads(index.read_text(encoding="utf-8"))
        planned = planned.get("columns", planned)
    else:
        planned = [
            {"id": path.stem, "backend": path.stem, "file": path.name}
            for path in sorted(
                aggregate.column_files(root),
                key=lambda p: (
                    FALLBACK_ORDER.index(p.stem)
                    if p.stem in FALLBACK_ORDER
                    else len(FALLBACK_ORDER),
                    p.stem,
                ),
            )
        ]

    # Alphabetically by the label a reader sees, not by the plan's run order.
    # The plan lists pyvisa-py first because it is the leg that runs on every
    # trigger, and the page inherited that as "the subject, leftmost, read
    # first" -- which is an editorial claim the column order should not be
    # making. Four implementations are being compared; none of them is the
    # premise.
    def _label_of(entry: dict) -> str:
        spec = backends.BACKENDS.get(entry.get("backend", entry.get("id", "")))
        return (entry.get("label") or (spec.name if spec else entry.get("id", "?"))).lower()

    cols: list[dict] = []
    for entry in sorted(planned, key=_label_of):
        spec = backends.BACKENDS.get(entry.get("backend", entry.get("id", "")))
        default_label = spec.name if spec else entry.get("id", "?")
        path = root / entry.get("file", f"{entry.get('id', '')}.json")

        column: dict
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            column = loaded[0] if isinstance(loaded, list) else loaded
        else:
            # Planned, absent. Never dropped: a table with a column silently
            # missing reads like agreement between the ones that remain.
            column = {"results": [], "notes": [], "context": {}}
            column.setdefault("status", "not-run")
            column.setdefault(
                "reason", entry.get("reason") or f"no report at {path.name}"
            )

        column["label"] = entry.get("label") or column.get("label") or default_label
        column["short"] = column["label"]
        # The backend id, not the label, is what identifies the subject: two
        # columns can be the same implementation on different platforms and
        # carry the same label.
        column["backend"] = entry.get("backend", entry.get("id", ""))
        for key in ("id", "os_label", "vendor_version", "status", "reason"):
            if entry.get(key):
                column[key] = entry[key]
        cols.append(column)
    return cols


def render_protocol(protocol: str, cols, out, prose: dict, source_url: str = "") -> None:
    w = out.append
    matrix = aggregate.build(cols)
    titles = prose.get("protocol_titles", {})

    # A row every implementation skipped is a row about the suite, not about
    # the implementations: usually a feature the transport does not have, so
    # all four say SKIP for the same reason and the row carries no comparison.
    # Left in the table they were pure noise -- and on VXI-11 there are a lot
    # of them. Hidden from the grid, listed under it: still auditable, since a
    # skip nobody can see is the failure mode this suite exists to avoid.
    hidden = {r.key for r in matrix.all_skipped}
    groups = {
        script: kept
        for script, rows in matrix.by_script().items()
        if (kept := [r for r in rows if r.key not in hidden])
    }
    by_column = matrix.unique_failures_by_column()
    shared = matrix.shared_failures()
    # Any row exactly one implementation fails, whichever it is. The marker
    # used to mean "pyvisa-py alone fails this"; it now means "one of these
    # four stands alone here", which is the interesting shape regardless of
    # whose column it is.
    unique_keys = {r.key for rows in by_column.values() for r in rows}
    unique = [r for r in matrix.rows if r.key in unique_keys]
    dead = [c for c in cols if aggregate.status(c) != "ok"]
    # Only worth showing when it separates two columns. Every leg is Linux
    # today, and stamping LINUX on all four of them is noise that reads as
    # information.
    show_os = len({c.get("os_label", "") for c in cols if c.get("os_label")}) > 1

    ctx = next(
        (c.get("context") for c in cols if aggregate.status(c) == "ok"), {}
    ) or {}
    # id on the section, so the lede above can link straight to a transport.
    # The protocol key is the anchor: it is already the stable identifier the
    # reports are filed under, and it does not move when a heading is reworded.
    w(f'<section class="proto-block" id="{esc(protocol)}">')
    w(f'<h2 class="proto">{esc(titles.get(protocol, protocol))}</h2>')
    lede = (
        f"{len(matrix.rows)} checks. {len(matrix.disagreements)} differ between "
        f"implementations, {len(unique)} of them failed by exactly one."
    )
    if shared:
        lede += (
            f" {len(shared)} are failed by all of them, which is usually the "
            f"suite's problem rather than theirs."
        )
    if matrix.all_skipped:
        lede += (
            f" {len(matrix.all_skipped)} could not run anywhere and are listed "
            f"below the table rather than in it."
        )
    # Stated separately from a dead column. These produced results and they are
    # trustworthy; it is the checks that are missing, and a gap reads as "not
    # applicable" unless something says otherwise.
    labels = aggregate.display_labels(cols)
    for i, column in enumerate(cols):
        if column.get("errors"):
            lede += (
                f" {labels[i]} is missing {len(column['errors'])} script(s) "
                f"that crashed ({', '.join(column['errors'])})."
            )
    if len(matrix.compared) < 2:
        # compare.py says the same thing when it is handed one backend. A grid
        # with a single column is a report, and calling its zero disagreements
        # a result would be claiming agreement with nobody.
        lede = (
            f"{len(matrix.rows)} checks from a single implementation. "
            f"Nothing was compared, so this section is a report rather than a "
            f"comparison."
        )
    if dead:
        # Said out loud, not just left as an empty column. The whole reason a
        # dead column is drawn at all is that its absence would read as
        # agreement between the implementations that did answer.
        shown = aggregate.display_labels(cols)
        names = ", ".join(
            shown[i] for i, c in enumerate(cols) if aggregate.status(c) != "ok"
        )
        lede += (
            f" {names} produced no results, so the counts above compare only "
            f"the rest."
        )
    w(f'<p class="lede">{esc(lede)}</p>')
    w('<div class="spec">'
      f'<span>resource <b>{esc(ctx.get("resource", "?"))}</b></span>'
      f'<span>host <b>{esc(ctx.get("platform", "?"))}</b></span>'
      "</div>")

    w('<div class="cards">')
    for i, column in enumerate(cols):
        st = aggregate.status(column)
        n = matrix.counts(i)
        p_, f_, s_ = n.get("PASS", 0), n.get("FAIL", 0), n.get("SKIP", 0)
        total = max(p_ + f_ + s_, 1)
        # The leg records this from the manifest entry it checksummed, so it
        # names the build that ran. It used to be a hand-written map in the
        # prose file, which meant the page kept claiming whichever version
        # someone last typed there -- and had no entry at all for Keysight,
        # whose library cannot simply be asked (get_library_paths() answers it
        # with an empty tuple).
        version = (
            column.get("vendor_version")
            or short_sha(column.get("context", {}).get("pyvisa-py commit", ""))
        )
        # The card's heading is already the implementation's name, and the
        # manifest spells its versions out in full ("Keysight IO Libraries
        # 21.3.94") because that string is also read on its own, in a job log.
        # Under the heading it stutters, so the name comes off here.
        if version.lower().startswith(column["short"].lower()):
            version = version[len(column["short"]):].strip() or version
        w(f'<article class="card{"" if st == "ok" else " dead"}">')
        w(f'<h3>{esc(column["short"])}</h3>')
        # Nested quotes in an f-string need 3.12; the vendor container is
        # Ubuntu 22.04 and ships 3.10, so keep the lookup outside.
        why = column.get("reason", "")
        sub = version if st == "ok" else f"{st} \u2014 {why}"
        # Omitted rather than rendered empty. A column whose build nobody
        # recorded should leave a gap, not an empty line the eye reads as a
        # version it cannot make out.
        if sub:
            w(f'<div class="ver">{esc(sub)}</div>')
        if show_os and column.get("os_label"):
            w(f'<div class="ver os">{esc(column["os_label"])}</div>')
        if column.get("errors"):
            w(f'<div class="ver crashed">{len(column["errors"])} script(s) '
              f'crashed &mdash; those checks are absent, not passing</div>')
        if st != "ok":
            w("</article>")
            continue
        w('<div class="counts">')
        w(f'<div class="count p"><b>{p_}</b><span>passed</span></div>')
        w(f'<div class="count f"><b>{f_}</b><span>failed</span></div>')
        w(f'<div class="count s"><b>{s_}</b><span>skipped</span></div>')
        w("</div>")
        w(f'<div class="bar"><i class="p" style="width:{100 * p_ / total:.1f}%"></i>'
          f'<i class="f" style="width:{100 * f_ / total:.1f}%"></i>'
          f'<i class="s" style="width:{100 * s_ / total:.1f}%"></i></div>')
        w("</article>")
    w("</div>")

    def head_cell(column: dict) -> str:
        # The OS belongs in the header. "Keysight on Windows disagrees with
        # PyVISA-py on Linux" is a weaker claim than the same-OS one, and a
        # column header that does not say which is which invites the stronger
        # reading.
        # Only when it distinguishes something. Every leg is Linux, so the
        # tag under every heading said the same word four times and cost the
        # narrow column its width.
        os_label = column.get("os_label") if show_os else None
        os_html = f'<span class="os">{esc(os_label)}</span>' if os_label else ""
        return f'<th class="st">{esc(column["short"])}{os_html}</th>'

    w('<div class="scroll"><table>')
    w("<thead><tr><th>Check</th>"
      + "".join(head_cell(c) for c in cols)
      + "</tr></thead><tbody>")
    row_id = 0
    for script, rows in groups.items():
        # The group header names the file and says what it cost each column.
        # Where a run's time goes is otherwise invisible, and it is not evenly
        # spread: 03_srq dominates, because VXI-11 service requests arrive
        # about one a second.
        times = []
        for column in cols:
            elapsed = column.get("elapsed") or {}
            secs = next(
                (v for k, v in elapsed.items() if rows and k in rows[0].name), None
            )
            if secs is not None:
                # Sub-second scripts are most of them; rounding to whole
                # seconds renders the fast ones as "0s" and hides the spread.
                shown = f"{secs:.1f}s" if secs < 10 else f"{secs:.0f}s"
                times.append(f"{aggregate.label(column)} {shown}")
        timing = (
            f'<span class="grp-time">{esc(" · ".join(times))}</span>' if times else ""
        )
        head = esc(script)
        if source_url and script:
            head = (
                f'<a class="src" href="{esc(source_url.rstrip("/"))}/{esc(script)}">'
                f"{head}</a>"
            )
        w(f'<tr class="grp"><td colspan="{len(cols) + 1}">{head}{timing}</td></tr>')
        for row in rows:
            row_id += 1
            uid = f"{protocol}-{row_id}"
            cells, details = [], []
            for result, column in zip(row.cells, cols):
                if result is None:
                    cells.append('<td class="st none">&mdash;</td>')
                    continue
                outcome = result["outcome"]
                # Every outcome, passes included. A pass records what it saw
                # in its detail -- the status code, the measured rate, the
                # value that came back -- and a row where four implementations
                # all say PASS for four different reasons is exactly the row
                # worth opening. Withholding it until something goes wrong
                # makes the page a grid of assertions you have to take on
                # trust.
                text = reason(result)
                if text:
                    details.append((column["short"], outcome, text))
                cells.append(f'<td class="st {outcome}">{outcome}</td>')

            classes = []
            if row.differs:
                classes.append("differs")
            elif row.all_skipped:
                classes.append("allskip")
            if row.key in unique_keys:
                classes.append("only-one")
            if details:
                classes.append("expandable")
            rule_html = (
                f'<span class="rule">{esc(row.rule)}</span>' if row.rule else ""
            )
            # The key, not the name. A name that carried a measurement would
            # be whichever column happened to be first, which is pyvisa-py --
            # so the row header would be quoting one implementation at the
            # others.
            name = esc(row.label)
            if source_url and row.source:
                href = f"{source_url.rstrip('/')}/{row.file}"
                if row.line:
                    href += f"#L{row.line}"
                # A small marker, not the whole label: the label is the thing
                # being read, and turning it into a link makes every row look
                # like a navigation element.
                name += (
                    f' <a class="src" href="{esc(href)}" '
                    f'title="{esc(row.source)}">[source]</a>'
                )

            if details:
                marker = '<span class="caret" aria-hidden="true">&#9656;</span>'
                w(f'<tr class="{" ".join(classes)}" data-row="{uid}" '
                  f'tabindex="0" role="button" aria-expanded="false" '
                  f'aria-controls="d-{uid}">'
                  f'<td class="check">{marker}{name}{rule_html}</td>'
                  f'{"".join(cells)}</tr>')
                panel = []
                for who, outcome, text in details:
                    head = summary_line(text)
                    panel.append(
                        f'<div class="detail-block">'
                        f'<div class="detail-head"><span class="tag {outcome}">'
                        f"{outcome}</span> {esc(who)}</div>"
                        + (f'<div class="detail-sum">{esc(head)}</div>'
                           if head and head != text else "")
                        + f"<pre>{esc(text)}</pre></div>"
                    )
                w(f'<tr class="detail-row" id="d-{uid}" hidden>'
                  f'<td colspan="{len(cols) + 1}">{"".join(panel)}</td></tr>')
            else:
                w(f'<tr class="{" ".join(classes)}">'
                  f'<td class="check"><span class="caret spacer"></span>'
                  f'{name}{rule_html}</td>{"".join(cells)}</tr>')
    w("</tbody></table></div>")

    def row_list(rows) -> None:
        w("<dl>")
        for row in rows:
            href = ""
            if source_url and row.source:
                href = f"{source_url.rstrip('/')}/{row.file}"
                if row.line:
                    href += f"#L{row.line}"
            name = esc(row.label)
            if href:
                name += (
                    f' <a class="src" href="{esc(href)}" '
                    f'title="{esc(row.source)}">[source]</a>'
                )
            w(f"<dt>{name}</dt>")
            reasons = sorted({reason(c) for c in row.cells if c and reason(c).strip()})
            w(f"<dd>{esc(summary_line(' / '.join(reasons))) or '&mdash;'}</dd>")
        w("</dl>")

    # Once per implementation, not once for the subject. A vendor failing what
    # the other three pass is the same finding as pyvisa-py doing it, and the
    # page said so for one of them only.
    if by_column or shared:
        w('<div class="standouts">')
        for index, rows in by_column.items():
            w("<details class=\"standout\">")
            w(f"<summary>{len(rows)} failure{'' if len(rows) == 1 else 's'} "
              f"unique to {esc(aggregate.label(cols[index]))}</summary>")
            w(f'<p class="lede">Checks {esc(aggregate.label(cols[index]))} '
              f"fails that every other implementation here passes.</p>")
            row_list(rows)
            w("</details>")
        if shared:
            w("<details class=\"standout\">")
            w(f"<summary>{len(shared)} failure{'' if len(shared) == 1 else 's'} "
              f"shared by every implementation</summary>")
            w('<p class="lede">Four independent implementations agreeing on a '
              "failure usually means the check is asserting more than the "
              "clause requires, or the mock is wrong. Read these as questions "
              "about the suite before questions about the libraries.</p>")
            row_list(shared)
            w("</details>")
        w("</div>")

    if matrix.all_skipped:
        w("<details class=\"skipped-all\">")
        n = len(matrix.all_skipped)
        w(f"<summary>{n} check{'' if n == 1 else 's'} no implementation could "
          f"run</summary>")
        w("<dl>")
        for row in matrix.all_skipped:
            reasons = sorted(
                {reason(c) for c in row.cells if c and reason(c).strip()}
            )
            w(f"<dt>{esc(row.label)}</dt>")
            w(f"<dd>{esc(' / '.join(reasons)) or '&mdash;'}</dd>")
        w("</dl></details>")

    w("</section>")


def load_prose(path: Path) -> dict:
    """The page's words: title, lede, per-protocol headings, footer.

    Out of this file on purpose. A CI run that has just produced a new set of
    results should not need a source edit to publish them, and a page's
    framing is prose -- it wants reviewing as prose, not as a Python literal.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reports",
        nargs="+",
        required=True,
        help="protocol=directory pairs, e.g. hislip=reports-hislip",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--prose",
        default=str(HERE / "docs" / "matrix.json"),
        help="JSON holding the page's title, lede and headings",
    )
    parser.add_argument(
        "--source-url",
        default="",
        metavar="PREFIX",
        help="link each check to its source, e.g. "
        "https://github.com/OWNER/REPO/blob/SHA . A row then reads back to the "
        "assertion that produced it instead of being grepped for",
    )
    parser.add_argument(
        "--full-page",
        action="store_true",
        help="emit a complete HTML document rather than body-only. The "
        "artifact host wants the body; GitHub Pages wants the document.",
    )
    args = parser.parse_args()

    prose = load_prose(Path(args.prose))

    sections = []
    for pair in args.reports:
        protocol, _, directory = pair.partition("=")
        cols = load(Path(directory))
        if not cols:
            print(f"no reports in {directory}")
            continue
        sections.append((protocol, cols))

    if not sections:
        return 4

    out: list[str] = []
    w = out.append
    if args.full_page:
        w("<!doctype html>")
        w('<html lang="en"><head><meta charset="utf-8">')
        w('<meta name="viewport" content="width=device-width,initial-scale=1">')
    # The artifact host reads the title from the first 8KB, and uses it as
    # the page's name in the tab and gallery. Keep it stable across redeploys.
    w(f'<title>{esc(prose.get("title", "VISA Conformance Matrix"))}</title>')
    w('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
      'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700'
      '&family=IBM+Plex+Sans:wght@400;500&display=swap">')
    w(f"<style>{STYLE}</style>")
    if args.full_page:
        w("</head><body>")
    w('<div class="wrap">')

    # The provenance block comes from the first column that actually ran. Taking
    # it from a column that produced nothing would stamp the page with a python
    # no result on it came from.
    #
    # pyvisa and python only: they are properties of the run, true of every
    # column on the page. The commit under test is not -- it belongs to the one
    # column built from a checkout, and it sits on that column's card. In the
    # masthead it read as the page's subject, which is not the framing this
    # page makes.
    first = sections[0][1]
    ran = [c.get("context") or {} for c in first if aggregate.status(c) == "ok"]
    ctx = ran[0] if ran else {}
    w('<header class="mast">')
    title = prose.get("title", "VISA Conformance Matrix")
    w(f"<h1>{esc(title)}</h1>")
    # Unescaped, like the footer: the lede links to the transport sections by
    # name, and the anchors belong in the prose beside the words they mark up.
    w(f'<p class="lede">{prose.get("lede", "")}</p>')
    w('<div class="spec">'
      f'<span>pyvisa <b>{esc(ctx.get("pyvisa", "?"))}</b></span>'
      f'<span>python <b>{esc(ctx.get("python", "?"))}</b></span>'
      "</div>")
    w("</header>")

    for protocol, cols in sections:
        render_protocol(protocol, cols, out, prose, args.source_url)

    w("""<script>
(function () {
  // Delegated so the handler count does not scale with the table, and so
  // keyboard users get the same affordance as the pointer: the rows are
  // role="button" and tabbable, which is meaningless without Enter/Space.
  function toggle(row) {
    var panel = document.getElementById('d-' + row.dataset.row);
    if (!panel) return;
    var open = row.getAttribute('aria-expanded') === 'true';
    row.setAttribute('aria-expanded', open ? 'false' : 'true');
    panel.hidden = open;
  }
  document.addEventListener('click', function (e) {
    var row = e.target.closest('tr.expandable');
    if (row) toggle(row);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest && e.target.closest('tr.expandable');
    if (!row) return;
    e.preventDefault();
    toggle(row);
  });
})();
</script>""")
    # From the prose file, like every other word on the page. The same text was
    # a literal here *and* a "footer" key nobody read, so editing the file --
    # the one place the file exists to be edited -- changed nothing.
    #
    # Not escaped: these strings carry their own markup, as the lede and the
    # protocol headings do. The file is part of the repository, not input.
    footer = prose.get("footer")
    if footer:
        w(f"<footer>{footer}</footer>")
    w("</div>")
    if args.full_page:
        w("</body></html>")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"{args.out}: {len(sections)} protocol section(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
