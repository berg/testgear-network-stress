#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract the normative statements from the specs, and say which are covered.

The specs are not in this repo and must not be -- they are IVI Foundation and
VXIbus Consortium copyright. This reads your own copies, so it needs
`--specs DIR` pointing at wherever they live, and it keeps only rule
identifiers and a short excerpt: enough to know what a rule is about and to
find it, not enough to be a copy of the document.

Coverage is computed against the `rule=` annotations already on the checks, so
a check that cites a clause is automatically counted and one that cites nothing
is automatically invisible. That is the intended pressure: an uncited check is
worth less, and this is where that shows up.

    tools/spec_rules.py --specs ~/specs --out docs/spec-coverage.md
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

#: The three documents, and how each writes a requirement.
#:
#: VPP-4.3 and VXI-11 use the VXIbus house style -- RULE / OBSERVATION /
#: RECOMMENDATION / PERMISSION, numbered. IVI-6.1 does not: it states
#: requirements as "shall" sentences inside numbered sections, so its
#: identifier has to be the section rather than a rule number.
SPECS = {
    "VPP-4.3": {
        "filenames": ("vpp43*.pdf", "VPP-4.3*.pdf"),
        "style": "vxibus",
        "title": "The VISA Library",
    },
    "VXI-11": {
        "filenames": ("vxi-11.pdf", "VXI-11.pdf"),
        "style": "vxibus",
        "title": "TCP/IP Instrument Protocol Specification",
    },
    "IVI-6.1": {
        "filenames": ("IVI-6.1*.pdf", "*HiSLIP*.pdf"),
        "style": "shall",
        "title": "HiSLIP",
    },
}

# VPP-4.3 numbers its rules 3.2.1; VXI-11 letters its appendix B.6.72. Both
# may carry a trailing colon. The identifier must contain a digit, which is
# what keeps the specs' own definition of the word ("RULE: Rules SHALL be
# followed...") out of the inventory.
VXIBUS_RE = re.compile(
    r"^\s*(RULE|OBSERVATION|RECOMMENDATION|PERMISSION)\s+"
    r"([A-Z]?\.?\d[\d.]*[A-Za-z]?)\s*:?\s*$"
)
SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\s+(\S.{2,60}?)\s*$")


def to_text(pdf: Path, cache: Path) -> str:
    """pdftotext -layout, cached. Layout mode keeps the rule headings on their
    own lines, which is what makes them findable at all."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and cache.stat().st_mtime >= pdf.stat().st_mtime:
        return cache.read_text(errors="replace")
    if shutil.which("pdftotext") is None:
        sys.exit("pdftotext is not installed (brew install poppler)")
    subprocess.run(
        ["pdftotext", "-layout", str(pdf), str(cache)], check=True, capture_output=True
    )
    return cache.read_text(errors="replace")


def parse_vxibus(text: str, spec: str) -> list[dict]:
    """RULE 3.2.3 followed by its indented body, until the next heading."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = VXIBUS_RE.match(lines[i])
        if not m:
            i += 1
            continue
        kind, ident = m.group(1), m.group(2).rstrip(".")
        body: list[str] = []
        j = i + 1
        while j < len(lines) and len(body) < 12:
            if VXIBUS_RE.match(lines[j]):
                break
            stripped = lines[j].strip()
            if stripped:
                body.append(stripped)
            elif body:
                break
            j += 1
        out.append(
            {
                "spec": spec,
                "kind": kind,
                "id": ident,
                "cite": f"{spec} {kind.title()} {ident}"
                if kind != "RULE"
                else f"{spec} {ident}",
                "text": " ".join(body),
            }
        )
        i = j
    return out


def parse_shall(text: str, spec: str) -> list[dict]:
    """A "shall" sentence, tagged with the section it sits in."""
    section, section_title = "?", ""
    out = []
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m and not m.group(2).endswith("."):
            section, section_title = m.group(1), m.group(2).strip()
            continue
        if re.search(r"\bshall\b", line, re.I) and len(line.strip()) > 30:
            out.append(
                {
                    "spec": spec,
                    "kind": "SHALL",
                    "id": section,
                    "cite": f"{spec} {section}",
                    "section": section_title,
                    "text": line.strip(),
                }
            )
    return out


#: A requirement that binds some other interface entirely -- no TCPIP client
#: can be held to it.
OTHER_INTERFACE = re.compile(
    r"\b(GPIB|USB|PXI|ASRL|serial|VXI backplane|backplane|VME|mainframe|slot|"
    r"register|SOCKET resource|INTFC|BACKPLANE|SERVANT|MEMACC|BERR)\b",
    re.IGNORECASE,
)
#: The register-access family. These name operations a *message-based* session
#: does not have -- viIn8, viMove, viMapAddress and the rest belong to
#: register-based resources -- so a rule about them binds no TCPIP INSTR
#: client. Without this they classify as client-testable purely because they
#: mention VI_ERROR, which put all 49 of VPP-4.3 6.3 in the queue by mistake.
REGISTER_BASED = re.compile(
    r"\bvi(In|Out|Move|Map|Unmap|Peek|Poke|MemAlloc|MemFree|Assert(Util|Intr)Signal)"
    r"[A-Za-z0-9]*\(\)"
)
#: A requirement on the instrument server rather than on the client.
SERVER_SIDE = re.compile(r"network instrument server SHALL|the server shall", re.IGNORECASE)
#: Anything naming an operation, attribute or status code a client can reach.
CLIENT_TESTABLE = re.compile(
    r"\bvi[A-Z]\w+|VI_ATTR_|VI_ERROR_|VI_SUCCESS|VI_WARN_|"
    r"\bclient shall\b|network instrument client SHALL"
)


def triage(rule: dict) -> str:
    """Which bucket an uncovered requirement falls in.

    The raw count of normative statements badly overstates what a client suite
    owes: most of VPP-4.3 is about interfaces this suite cannot reach, and much
    of VXI-11 binds the server. Sorting them is what turns 854 into a queue.
    """
    text = rule.get("text", "")
    if REGISTER_BASED.search(text):
        return "other interface"
    if OTHER_INTERFACE.search(text) and not re.search(
        r"TCPIP|HiSLIP|VXI-11", text, re.IGNORECASE
    ):
        return "other interface"
    if SERVER_SIDE.search(text):
        return "server-side"
    if CLIENT_TESTABLE.search(text):
        return "client-testable"
    return "prose or definitional"


def cited_rules() -> dict[str, list[str]]:
    """Every `rule=` string in the checks, mapped to the checks citing it."""
    cites: dict[str, list[str]] = {}
    for path in sorted((HERE / "checks").glob("*.py")):
        source = path.read_text()
        for m in re.finditer(r'rule\s*=\s*"([^"]+)"', source):
            cites.setdefault(m.group(1), []).append(path.name)
    return cites


def normalise(cite: str) -> str:
    """`VPP-4.3 RULE 6.1.1` and `VPP-4.3 6.1.1` are the same clause."""
    c = cite.upper().replace("RULE", "").replace("SECTION", "")
    return re.sub(r"[^A-Z0-9.]+", " ", c).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--specs",
        required=True,
        help="directory holding your copies of the spec PDFs (searched recursively)",
    )
    parser.add_argument("--cache", default=None, help="where to keep extracted text")
    parser.add_argument("--out", default=None, help="write the coverage report here")
    parser.add_argument("--json", default=None, help="write the rule inventory here")
    args = parser.parse_args()

    specs_dir = Path(args.specs).expanduser()
    cache = Path(args.cache).expanduser() if args.cache else specs_dir / ".text-cache"

    rules: list[dict] = []
    found_specs = {}
    for spec, meta in SPECS.items():
        pdf = None
        for pattern in meta["filenames"]:
            hits = sorted(specs_dir.rglob(pattern))
            if hits:
                pdf = hits[0]
                break
        if pdf is None:
            print(f"!! {spec} not found under {specs_dir} (looked for "
                  f"{', '.join(meta['filenames'])})", file=sys.stderr)
            continue
        found_specs[spec] = pdf
        text = to_text(pdf, cache / f"{spec}.txt")
        parsed = (
            parse_vxibus(text, spec)
            if meta["style"] == "vxibus"
            else parse_shall(text, spec)
        )
        rules.extend(parsed)
        print(f"{spec}: {len(parsed)} statements from {pdf.name}")

    if not rules:
        return 4

    cites = cited_rules()
    cite_index = {normalise(c): (c, v) for c, v in cites.items()}
    for rule in rules:
        key = normalise(rule["cite"])
        hit = cite_index.get(key)
        # A rule is also covered when a check cites the section it lives in.
        if hit is None:
            for ck, (orig, checks) in cite_index.items():
                if ck and (key.startswith(ck) or ck.startswith(key)):
                    hit = (orig, checks)
                    break
        rule["covered_by"] = sorted(set(hit[1])) if hit else []

    if args.json:
        Path(args.json).write_text(json.dumps(rules, indent=2))
        print(f"inventory written to {args.json}")

    covered = [r for r in rules if r["covered_by"]]
    print(f"\n{len(rules)} normative statements, {len(covered)} touched by a check")
    buckets: dict[str, int] = {}
    for rule in rules:
        if rule["covered_by"] or rule["kind"] not in ("RULE", "SHALL"):
            continue
        bucket = triage(rule)
        rule["bucket"] = bucket
        buckets[bucket] = buckets.get(bucket, 0) + 1
    for bucket, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {count:4} uncovered: {bucket}")
    for spec in found_specs:
        s = [r for r in rules if r["spec"] == spec]
        c = [r for r in s if r["covered_by"]]
        print(f"  {spec:9} {len(c):3}/{len(s):3} covered")

    if args.out:
        write_report(Path(args.out), rules, found_specs, cites)
        print(f"report written to {args.out}")
    return 0


def write_report(out: Path, rules, found_specs, cites) -> None:
    covered = [r for r in rules if r["covered_by"]]
    lines = [
        "# Spec coverage",
        "",
        "Generated by `tools/spec_rules.py` from local copies of the specs, which",
        "are **not** in this repo and must not be. Rule text is excerpted only far",
        "enough to say what a rule is about; read the clause in your own copy.",
        "",
        f"{len(rules)} normative statements found, {len(covered)} currently touched",
        "by at least one check.",
        "",
        "That raw ratio is not the interesting one. Most of these requirements",
        "bind interfaces a TCPIP client cannot reach, or the instrument server,",
        "or state no single observable behaviour. Against the requirements a",
        "TCPIP INSTR **client** can actually be held to:",
        "",
        f"> **{len(covered)} of {len(covered) + sum(1 for r in rules if r.get('bucket') == 'client-testable')} "
        "covered.**",
        "",
        "| spec | document | statements | cited by a check |",
        "| --- | --- | --- | --- |",
    ]
    for spec, pdf in found_specs.items():
        s = [r for r in rules if r["spec"] == spec]
        c = [r for r in s if r["covered_by"]]
        lines.append(
            f"| {spec} | {SPECS[spec]['title']} | {len(s)} | {len(c)} |"
        )
    lines += [
        "",
        "## What is left",
        "",
        "The raw count overstates what a client suite owes. Uncovered",
        "requirements, triaged:",
        "",
        "| bucket | count | meaning |",
        "| --- | --- | --- |",
        f"| client-testable | {sum(1 for r in rules if r.get('bucket') == 'client-testable')} "
        "| the actual queue |",
        f"| prose or definitional | {sum(1 for r in rules if r.get('bucket') == 'prose or definitional')} "
        "| no single observable behaviour, or defines a term |",
        f"| other interface | {sum(1 for r in rules if r.get('bucket') == 'other interface')} "
        "| GPIB, USB, PXI, serial, VXI backplane |",
        f"| server-side | {sum(1 for r in rules if r.get('bucket') == 'server-side')} "
        "| binds the instrument server, not the client |",
        "",
        "Coverage is computed from the `rule=` annotations on the checks, so a",
        "check citing no clause counts for nothing here. That is deliberate: the",
        "vendor comparison found that every uncited check in its disputed set was",
        "the check's own fault, so an uncited check has not earned coverage.",
        "",
        "The hand-written gap analysis lives in "
        "[`spec-gaps.md`](spec-gaps.md); this file is generated and would",
        "overwrite it.",
        "",
        "## Cited clauses",
        "",
        "| clause | checks |",
        "| --- | --- |",
    ]
    for cite in sorted(cites):
        lines.append(f"| `{cite}` | {', '.join(sorted(set(cites[cite])))} |")
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
