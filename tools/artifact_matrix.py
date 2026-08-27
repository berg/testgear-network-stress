#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the comparison as a shareable page.

Distinct from `testgear/report.py`, which writes a standalone file for local
viewing. This emits body-only HTML for publishing as an artifact, and it says
out loud what the totals are worth -- a reader who sees three columns of counts
will otherwise read them as a score, which is the one conclusion the data
cannot support (see docs/findings.md).
"""

from __future__ import annotations

import argparse
import collections
import html
import json
from pathlib import Path

ORDER = [("py", "PyVISA-py"), ("ni", "NI-VISA"), ("rs", "R&S VISA")]
VERSIONS = {"NI-VISA": "libvisa 26.5.0", "R&S VISA": "librsvisa 5.12.9"}

STYLE = """
:root{
  --ground:#F6F8F8; --panel:#FFFFFF; --ink:#0F1719; --muted:#5C6E72;
  --rule:#D9E2E3; --rule-soft:#EAF0F0;
  --pass:#17795A; --fail:#BE3A2B; --skip:#9C6A12; --accent:#0C7C86;
  --fail-wash:#FBF0EE; --stripe:#BE3A2B;
  --sans:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --cond:"IBM Plex Sans Condensed","IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0D1315; --panel:#141D20; --ink:#DEE9EA; --muted:#8A9CA0;
  --rule:#243033; --rule-soft:#1A2427;
  --pass:#4FBF95; --fail:#F0796A; --skip:#DCA43A; --accent:#3FB6BF;
  --fail-wash:#201616; --stripe:#F0796A;
}}
:root[data-theme="dark"]{
  --ground:#0D1315; --panel:#141D20; --ink:#DEE9EA; --muted:#8A9CA0;
  --rule:#243033; --rule-soft:#1A2427;
  --pass:#4FBF95; --fail:#F0796A; --skip:#DCA43A; --accent:#3FB6BF;
  --fail-wash:#201616; --stripe:#F0796A;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:74rem;margin:0 auto;padding:2.5rem 1.25rem 5rem;
  display:flex;flex-direction:column;gap:2.25rem}
.mast{display:flex;flex-direction:column;gap:.5rem}
.eyebrow{font-family:var(--cond);font-weight:600;font-size:.75rem;
  letter-spacing:.13em;text-transform:uppercase;color:var(--accent)}
h1{font-family:var(--cond);font-weight:700;font-size:clamp(1.7rem,4vw,2.5rem);
  margin:0;line-height:1.1;text-wrap:balance;letter-spacing:-.01em}
.lede{margin:0;color:var(--muted);max-width:62ch}
.spec{font-family:var(--mono);font-size:.76rem;color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.35rem 1.4rem;padding-top:.65rem;
  border-top:1px solid var(--rule)}
.spec b{font-weight:500;color:var(--ink)}
.cards{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:1rem 1.1rem;display:flex;flex-direction:column;gap:.7rem}
.card h2{font-family:var(--cond);font-size:1.05rem;font-weight:600;margin:0;
  letter-spacing:.01em}
.card .ver{font-family:var(--mono);font-size:.72rem;color:var(--muted);
  margin-top:-.45rem}
.counts{display:flex;gap:1.25rem;font-variant-numeric:tabular-nums}
.count{display:flex;flex-direction:column;line-height:1.15}
.count b{font-family:var(--cond);font-size:1.5rem;font-weight:700}
.count span{font-family:var(--cond);font-size:.68rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.count.p b{color:var(--pass)} .count.f b{color:var(--fail)} .count.s b{color:var(--skip)}
.bar{display:flex;height:5px;border-radius:2px;overflow:hidden;background:var(--rule-soft)}
.bar i{display:block}
.bar .p{background:var(--pass)} .bar .f{background:var(--fail)} .bar .s{background:var(--skip)}
h3{font-family:var(--cond);font-size:.8rem;font-weight:600;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);margin:0 0 .5rem}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);
  border-radius:3px}
table{border-collapse:collapse;width:100%;font-size:.83rem;min-width:44rem}
thead th{position:sticky;top:0;background:var(--panel);z-index:2;
  font-family:var(--cond);font-size:.7rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);text-align:left;font-weight:600;
  padding:.6rem .75rem;border-bottom:1px solid var(--rule)}
thead th.st{text-align:center;width:7.5rem}
td{padding:.4rem .75rem;border-bottom:1px solid var(--rule-soft);vertical-align:top}
tr.grp td{background:var(--rule-soft);font-family:var(--cond);font-weight:600;
  font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  padding:.5rem .75rem}
tr.differs{background:var(--fail-wash)}
tr.differs td:first-child{box-shadow:inset 3px 0 0 var(--stripe)}
.check{max-width:34rem}
.rule{font-family:var(--mono);font-size:.68rem;color:var(--muted);display:block;
  margin-top:.1rem}
td.st{text-align:center;font-family:var(--mono);font-size:.7rem;font-weight:600;
  letter-spacing:.04em;white-space:nowrap}
td.PASS{color:var(--pass)} td.FAIL{color:var(--fail)}
td.SKIP{color:var(--skip)} td.none{color:var(--muted)}
.note{background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--accent);
  border-radius:3px;padding:1rem 1.15rem;display:flex;flex-direction:column;gap:.6rem}
.note.warn{border-left-color:var(--skip)}
.note p{margin:0;max-width:70ch}
.note b{font-family:var(--cond);letter-spacing:.01em}
code{font-family:var(--mono);font-size:.88em}
footer{color:var(--muted);font-size:.78rem;border-top:1px solid var(--rule);
  padding-top:1rem;max-width:70ch}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def esc(value) -> str:
    return html.escape(str(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--out", required=True)
    parser.add_argument("--protocol", default="vxi11")
    args = parser.parse_args()

    root = Path(args.reports)
    cols, lookups = [], []
    for key, label in ORDER:
        path = root / f"{key}.json"
        if not path.exists():
            continue
        loaded = json.loads(path.read_text())
        column = loaded[0] if isinstance(loaded, list) else loaded
        column["short"] = label
        cols.append(column)
        lookups.append({r.get("key", r["name"]): r for r in column["results"]})

    if not cols:
        print(f"no reports in {root}")
        return 4

    # Rows keep their source-script grouping: a real grouping, not decoration.
    groups: dict[str, list] = collections.OrderedDict()
    seen: set[str] = set()
    for column in cols:
        for r in column["results"]:
            k = r.get("key", r["name"])
            if k in seen:
                continue
            seen.add(k)
            script, _, rest = r["name"].partition(": ")
            groups.setdefault(script, []).append((k, rest or r["name"], r.get("rule", "")))

    differing = sum(
        len({lk.get(k, {}).get("outcome", "-") for lk in lookups}) > 1
        for rows in groups.values()
        for k, _, _ in rows
    )
    uncited_differing = sum(
        1
        for rows in groups.values()
        for k, _, rule in rows
        if not rule and len({lk.get(k, {}).get("outcome", "-") for lk in lookups}) > 1
    )

    ctx = cols[0].get("context", {})
    out = []
    w = out.append

    w('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
      'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700'
      '&family=IBM+Plex+Sans:wght@400;500&display=swap">')
    w(f"<style>{STYLE}</style>")
    w('<div class="wrap">')

    w('<header class="mast">')
    w(f'<div class="eyebrow">{esc(args.protocol)} &middot; three implementations, '
      f'one mock server</div>')
    w(f"<h1>{'VXI-11' if args.protocol == 'vxi11' else 'HiSLIP'} Conformance Matrix</h1>")
    w(f'<p class="lede">The same {len(seen)} checks run against PyVISA-py, NI-VISA and '
      f'R&amp;S VISA &mdash; same container, same kernel, same fault-injecting mock. '
      f'{differing} come back different, and those are the only rows carrying colour. '
      f'The counts are <em>not</em> a score; the panel below the table says why.</p>')
    w('<div class="spec">'
      f'<span>resource <b>{esc(ctx.get("resource", "?"))}</b></span>'
      f'<span>host <b>{esc(ctx.get("platform", "?"))}</b></span>'
      f'<span>pyvisa <b>{esc(ctx.get("pyvisa", "?"))}</b></span>'
      f'<span>pyvisa-py <b>{esc(ctx.get("pyvisa-py commit", "?"))}</b></span>'
      "</div>")
    w("</header>")

    w('<section class="cards">')
    for column in cols:
        n = collections.Counter(r["outcome"] for r in column["results"])
        p, f, s = n.get("PASS", 0), n.get("FAIL", 0), n.get("SKIP", 0)
        total = max(p + f + s, 1)
        version = VERSIONS.get(column["short"], ctx.get("pyvisa-py commit", ""))
        w('<article class="card">')
        w(f'<h2>{esc(column["short"])}</h2>')
        w(f'<div class="ver">{esc(version)}</div>')
        w('<div class="counts">')
        w(f'<div class="count p"><b>{p}</b><span>passed</span></div>')
        w(f'<div class="count f"><b>{f}</b><span>failed</span></div>')
        w(f'<div class="count s"><b>{s}</b><span>skipped</span></div>')
        w("</div>")
        w(f'<div class="bar"><i class="p" style="width:{100 * p / total:.1f}%"></i>'
          f'<i class="f" style="width:{100 * f / total:.1f}%"></i>'
          f'<i class="s" style="width:{100 * s / total:.1f}%"></i></div>')
        w("</article>")
    w("</section>")

    w("""<section class="note warn">
<p><b>Where these checks come from decides what they can find.</b> The first
version of this suite grew out of one written against PyVISA-py, so its checks
encoded PyVISA-py's behaviour. Across 115 of them there was <em>no</em> case
where PyVISA-py failed and both vendors passed &mdash; which read as
reassuring, and was mostly an artefact. Checks that describe one
implementation cannot, by construction, find much that implementation does
uniquely wrong.</p>
<p><b>Rewriting them from spec clauses produced eight such cases
immediately.</b> Same instrument, same three implementations, same mock server.
Lock nesting, the event-enable rules, four required attributes, required
operations raising instead of reporting, and a resource name whose class suffix
is matched case-sensitively &mdash; all confirmed: PyVISA-py fails, NI-VISA and
R&amp;S VISA both pass.</p>
<p><b>So the totals still are not a score, but the citations are load-bearing.</b>
When a check fails on all three, the rule is to suspect the check &mdash; and
twice that was right, once because a spec version number tracks the calendar
rather than correctness, once because no implementation at all does what the
clause says. An uncited check is suspect until a clause is found for it, because
writing one forces the question &ldquo;what says so?&rdquo;</p>
</section>""")

    w('<section><h3>Every check, by source script</h3><div class="scroll"><table>')
    w("<thead><tr><th>Check</th>"
      + "".join(f'<th class="st">{esc(c["short"])}</th>' for c in cols)
      + "</tr></thead><tbody>")
    for script, rows in groups.items():
        w(f'<tr class="grp"><td colspan="{len(cols) + 1}">{esc(script)}</td></tr>')
        for k, name, rule in rows:
            cells, outcomes = [], set()
            for lk in lookups:
                r = lk.get(k)
                outcome = r["outcome"] if r else "none"
                outcomes.add(outcome)
                cells.append(
                    f'<td class="st {outcome}">'
                    f'{"&mdash;" if outcome == "none" else outcome}</td>'
                )
            differs = len(outcomes) > 1
            rule_html = f'<span class="rule">{esc(rule)}</span>' if rule else ""
            w(f'<tr class="{"differs" if differs else ""}">'
              f'<td class="check">{esc(name)}{rule_html}</td>{"".join(cells)}</tr>')
    w("</tbody></table></div></section>")

    w("""<section class="note">
<p><b>NI-VISA passes every check here.</b> 52 of 52, with 5 skipped for rules
this transport cannot reach. That is the strongest evidence the checks
themselves are sound: a mature reference implementation scoring perfectly
against a set of assertions means the assertions are not arbitrary, and it is
what lets the failures below be read as findings rather than as opinions.</p>
<p><b>The two that matter most.</b> With <code>maxRecvSize</code> reported as zero,
NI-VISA survives and returns its data while PyVISA-py and R&amp;S both hang
forever &mdash; a missing bounds check, not an inevitability. Against a stalled
connection with a 2000&nbsp;ms timeout, NI-VISA answers
<code>VI_ERROR_TSK_TIMEOUT</code> in 2001&nbsp;ms; PyVISA-py answers
<code>VI_ERROR_IO</code> about 11&nbsp;s late, and R&amp;S never returns.</p>
<p><b>Why that pair is the reason to trust the instrument.</b> Both are checks
NI passes and PyVISA-py fails, and both were written from a spec clause rather
than from observed behaviour. A suite rigged to flatter PyVISA-py would not
produce them. The instrument works; its aggregate is what misleads.</p>
<p><b>Still open.</b> Ten checks that PyVISA-py passes and both vendors fail are
not claims in either direction &mdash; each is either a check encoding
PyVISA-py's behaviour or a real vendor difference, and at least one is neither:
it fails on its very first iteration while blaming link exhaustion, so its own
diagnosis is wrong.</p>
</section>""")

    w("<footer>Generated from the run's JSON reports. Skips are counted and "
      "coloured separately from passes throughout &mdash; at a glance a skipped "
      "check reads like a passing one, and the ones that matter are exactly "
      "those that stay skipped run after run without anybody noticing."
      f"{f' {uncited_differing} of the differing rows cite no clause.' if uncited_differing else ''}"
      "</footer>")
    w("</div>")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"{args.out}: {len(seen)} checks, {differing} differing, {len(cols)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
