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
"""

from __future__ import annotations

import argparse
import collections
import html
import json
from pathlib import Path

ORDER = [("py", "PyVISA-py"), ("ni", "NI-VISA"), ("rs", "R&S VISA")]
VERSIONS = {"NI-VISA": "libvisa 26.5.0", "R&S VISA": "librsvisa 5.12.9"}
TITLES = {"hislip": "HiSLIP", "vxi11": "VXI-11"}

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
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:76rem;margin:0 auto;padding:2.5rem 1.25rem 5rem;
  display:flex;flex-direction:column;gap:2.5rem}
.mast{display:flex;flex-direction:column;gap:.5rem}
.eyebrow{font-family:var(--cond);font-weight:600;font-size:.75rem;
  letter-spacing:.13em;text-transform:uppercase;color:var(--accent)}
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
tr.allskip{background:var(--skip-wash)}
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
.detail-head{font-family:var(--cond);font-size:.78rem;font-weight:600;
  letter-spacing:.04em;color:var(--muted);margin-bottom:.15rem}
.detail-head .tag{font-family:var(--mono);font-size:.68rem;font-weight:700;
  letter-spacing:.05em;margin-right:.45rem}
.detail-head .tag.FAIL{color:var(--fail)}
.detail-head .tag.SKIP{color:var(--skip)}
.detail-sum{font-size:.82rem;margin-bottom:.4rem;max-width:80ch}
.detail-block pre{font-family:var(--mono);font-size:.72rem;line-height:1.5;
  margin:0;padding:.6rem .7rem;background:var(--panel);
  border:1px solid var(--rule);border-radius:3px;
  overflow-x:auto;white-space:pre;color:var(--ink)}
.findings{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:1rem 1.15rem}
.findings table{min-width:34rem;font-size:.82rem}
.findings td:first-child{font-family:var(--mono);font-size:.74rem;white-space:nowrap;
  color:var(--muted);vertical-align:top}
.findings .scroll{border:0;background:transparent}
code{font-family:var(--mono);font-size:.88em}
footer{color:var(--muted);font-size:.78rem;border-top:1px solid var(--rule);
  padding-top:1rem;max-width:70ch}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def esc(value) -> str:
    return html.escape(str(value))


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


def load(root: Path):
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
    return cols, lookups


def render_protocol(protocol: str, cols, lookups, out) -> None:
    w = out.append
    groups: dict[str, list] = collections.OrderedDict()
    seen: set[str] = set()
    for column in cols:
        for r in column["results"]:
            k = r.get("key", r["name"])
            if k in seen:
                continue
            seen.add(k)
            script, _, rest = r["name"].partition(": ")
            groups.setdefault(script, []).append(
                (k, rest or r["name"], r.get("rule", ""))
            )

    differing = pyvisa_only = skipped_rows = 0
    for rows in groups.values():
        for k, _, _ in rows:
            outcomes = [lk.get(k, {}).get("outcome", "-") for lk in lookups]
            if len(set(outcomes)) > 1:
                differing += 1
            if set(outcomes) == {"SKIP"}:
                skipped_rows += 1
            if (
                len(outcomes) == 3
                and outcomes[0] == "FAIL"
                and outcomes[1] == "PASS"
                and outcomes[2] == "PASS"
            ):
                pyvisa_only += 1

    ctx = cols[0].get("context", {})
    w('<section class="proto-block">')
    w(f'<h2 class="proto">{esc(TITLES.get(protocol, protocol))}</h2>')
    lede = (
        f"{len(seen)} checks. {differing} differ between implementations, "
        f"{pyvisa_only} of them failures unique to PyVISA-py."
    )
    if skipped_rows:
        lede += f" {skipped_rows} could not run anywhere."
    w(f'<p class="lede">{esc(lede)}</p>')
    w('<div class="spec">'
      f'<span>resource <b>{esc(ctx.get("resource", "?"))}</b></span>'
      f'<span>host <b>{esc(ctx.get("platform", "?"))}</b></span>'
      "</div>")

    w('<div class="cards">')
    for column in cols:
        n = collections.Counter(r["outcome"] for r in column["results"])
        p, f, s = n.get("PASS", 0), n.get("FAIL", 0), n.get("SKIP", 0)
        total = max(p + f + s, 1)
        version = VERSIONS.get(column["short"], ctx.get("pyvisa-py commit", ""))
        w('<article class="card">')
        w(f'<h3>{esc(column["short"])}</h3>')
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
    w("</div>")

    w('<div class="scroll"><table>')
    w("<thead><tr><th>Check</th>"
      + "".join(f'<th class="st">{esc(c["short"])}</th>' for c in cols)
      + "</tr></thead><tbody>")
    row_id = 0
    for script, rows in groups.items():
        w(f'<tr class="grp"><td colspan="{len(cols) + 1}">{esc(script)}</td></tr>')
        for k, name, rule in rows:
            row_id += 1
            uid = f"{protocol}-{row_id}"
            cells, outcomes, details = [], set(), []
            for lk, column in zip(lookups, cols):
                r = lk.get(k)
                if r is None:
                    outcomes.add("none")
                    cells.append('<td class="st none">&mdash;</td>')
                    continue
                outcome = r["outcome"]
                outcomes.add(outcome)
                text = reason(r) if outcome != "PASS" else ""
                if text:
                    details.append((column["short"], outcome, text))
                cells.append(f'<td class="st {outcome}">{outcome}</td>')

            classes = []
            if len(outcomes) > 1:
                classes.append("differs")
            elif outcomes == {"SKIP"}:
                classes.append("allskip")
            if details:
                classes.append("expandable")
            rule_html = f'<span class="rule">{esc(rule)}</span>' if rule else ""

            if details:
                marker = '<span class="caret" aria-hidden="true">&#9656;</span>'
                w(f'<tr class="{" ".join(classes)}" data-row="{uid}" '
                  f'tabindex="0" role="button" aria-expanded="false" '
                  f'aria-controls="d-{uid}">'
                  f'<td class="check">{marker}{esc(name)}{rule_html}</td>'
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
                  f'{esc(name)}{rule_html}</td>{"".join(cells)}</tr>')
    w("</tbody></table></div>")
    w("</section>")


FINDINGS = [
    ("VPP-4.3 3.2.3", "<code>VI_ATTR_RSRC_SPEC_VERSION</code> is unsupported"),
    ("VPP-4.3 3.2.5", "<code>VI_ATTR_MAX_QUEUE_LENGTH</code> is unsupported"),
    ("VPP-4.3 3.3.2",
     "<code>viClose(VI_NULL)</code> answers <code>VI_ERROR_INV_OBJECT</code> "
     "rather than <code>VI_WARN_NULL_OBJECT</code>"),
    ("VPP-4.3 3.4.2",
     "a termination character of <code>0x1FF</code> is stored as <code>511</code>; "
     "both vendors mask it to a byte"),
    ("VPP-4.3 3.6.17",
     "a 300-character shared-lock key is accepted, where 256 or more is an error"),
    ("VPP-4.3 3.6.28",
     "a nested exclusive lock is not reported; over VXI-11 the session waits out "
     "its own timeout against a lock it already holds"),
    ("VPP-4.3 3.6.32", "an unlock that leaves a lock still held is not reported"),
    ("VPP-4.3 3.7.6",
     "<code>viEnableEvent(VI_HNDLR)</code> succeeds with no handler installed"),
    ("VPP-4.3 3.7.13",
     "<code>VI_SUSPEND_HNDLR | VI_HNDLR</code> is accepted; the modes are "
     "mutually exclusive"),
    ("VPP-4.3 4.3.17",
     "the resource class suffix is matched case-sensitively &mdash; "
     "<code>::instr</code> is refused where <code>::INSTR</code> opens"),
    ("VPP-4.3 5.1.12", "four required message-based attributes are missing"),
    ("VPP-4.3 5.1.17",
     "<code>VI_ATTR_TCPIP_PORT</code> and <code>VI_ATTR_TCPIP_NODELAY</code> "
     "are missing"),
    ("VPP-4.3 5.1.72",
     "required operations raise a Python exception instead of returning a status"),
    ("VXI-11 B.6.3",
     "a <code>maxRecvSize</code> of zero wedges the session &mdash; "
     "<code>viWrite</code> never returns"),
    ("VPP-4.3 3.2.2",
     "a stalled connection answers <code>VI_ERROR_IO</code> about 11&nbsp;s late; "
     "NI-VISA answers <code>VI_ERROR_TSK_TIMEOUT</code> at 2001&nbsp;ms"),
]

VENDOR_FINDINGS = [
    ("R&amp;S &middot; VPP-4.3 3.7.21",
     "<code>viWaitOnEvent</code> does not dequeue an event whose type was "
     "disabled after it arrived"),
    ("R&amp;S &middot; IVI-6.1 2.6",
     "shared locks over HiSLIP are refused outright with "
     "<code>VI_ERROR_INV_PROTOCOL</code>"),
    ("all three &middot; VPP-4.3 5.1.11",
     "<code>VI_ATTR_TRIG_ID</code> is required of every INSTR resource and "
     "supported by none of them on TCPIP"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reports",
        nargs="+",
        required=True,
        help="protocol=directory pairs, e.g. hislip=reports-hislip",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sections = []
    for pair in args.reports:
        protocol, _, directory = pair.partition("=")
        cols, lookups = load(Path(directory))
        if not cols:
            print(f"no reports in {directory}")
            continue
        sections.append((protocol, cols, lookups))

    if not sections:
        return 4

    out: list[str] = []
    w = out.append
    # The artifact host reads the title from the first 8KB, and uses it as
    # the page's name in the tab and gallery. Keep it stable across redeploys.
    w("<title>VISA Conformance Matrix</title>")
    w('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
      'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700'
      '&family=IBM+Plex+Sans:wght@400;500&display=swap">')
    w(f"<style>{STYLE}</style>")
    w('<div class="wrap">')

    ctx = sections[0][1][0].get("context", {})
    w('<header class="mast">')
    w('<div class="eyebrow">PyVISA-py &middot; NI-VISA &middot; R&amp;S VISA</div>')
    w("<h1>VISA Conformance Matrix</h1>")
    w('<p class="lede">Spec-cited conformance checks over HiSLIP and VXI-11, run '
      'from one container against one fault-injecting mock server. Rows where the '
      'implementations disagree carry colour. Click any row with a caret for the '
      'full failure or skip detail.</p>')
    w('<div class="spec">'
      f'<span>pyvisa <b>{esc(ctx.get("pyvisa", "?"))}</b></span>'
      f'<span>pyvisa-py <b>{esc(ctx.get("pyvisa-py commit", "?"))}</b></span>'
      f'<span>python <b>{esc(ctx.get("python", "?"))}</b></span>'
      "</div>")
    w("</header>")

    for protocol, cols, lookups in sections:
        render_protocol(protocol, cols, lookups, out)

    w('<section class="findings">')
    w("<h4>Confirmed findings</h4>")
    w('<p class="lede" style="margin-bottom:.8rem">Checks PyVISA-py fails that '
      "both NI-VISA and R&amp;S VISA pass.</p>")
    w('<div class="scroll"><table><tbody>')
    for clause, text in FINDINGS:
        w(f"<tr><td>{clause}</td><td>{text}</td></tr>")
    w("</tbody></table></div></section>")

    w('<section class="findings">')
    w("<h4>Findings elsewhere</h4>")
    w('<div class="scroll"><table><tbody>')
    for who, text in VENDOR_FINDINGS:
        w(f"<tr><td>{who}</td><td>{text}</td></tr>")
    w("</tbody></table></div></section>")

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
    w("<footer>Skips are counted and coloured separately from passes throughout. "
      "A skipped check reads like a passing one at a glance, and the ones that "
      "matter are those that stay skipped run after run &mdash; so every skip "
      "carries its reason.</footer>")
    w("</div>")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"{args.out}: {len(sections)} protocol section(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
