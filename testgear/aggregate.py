# SPDX-License-Identifier: GPL-3.0-or-later
"""Folding runs into columns, and columns into a matrix.

Four places grew their own copy of the same join -- `compare.py`, both tools in
`tools/`, and `report.render_matrix` -- and they had already drifted in the
details that matter: whether a column that produced nothing appears at all, and
whether a row where one implementation is simply absent counts as a
disagreement. Those are not rendering details. They decide what the published
page claims.

The join itself is on `harness.stable_key`, never on the displayed name. Those
are the same string today -- a check name is a static title and its evidence
lives in `detail` -- but the distinction is the contract: a name that varies
with its outcome splits into one row per backend, each showing a gap where the
others answered, and keeping the join on the key is what makes that fixable in
one place.
"""

from __future__ import annotations

import collections
import dataclasses

#: A column that did not produce results still gets one of these, so the
#: matrix can say why. `ok` is the only status whose outcomes are compared;
#: see `Row.differs`.
STATUSES = ("ok", "unavailable", "errored", "not-run")


#: Files that live beside the column reports and are not columns. Both the
#: matrix tools glob `*.json`, and without this an exit-code sidecar written by
#: the same run would be loaded as an implementation named "ni.rc" -- a column
#: with no results, which is exactly the shape this code is careful to treat as
#: "something did not run".
NOT_COLUMNS = ("index.json", "leg.json", "plan.json")
NOT_COLUMN_SUFFIXES = (".rc.json",)


def column_files(root) -> list:
    """The report files in a directory, in name order, sidecars excluded."""
    from pathlib import Path

    return sorted(
        p
        for p in Path(root).glob("*.json")
        if p.name not in NOT_COLUMNS
        and not any(p.name.endswith(sfx) for sfx in NOT_COLUMN_SUFFIXES)
    )


def merge(reports: list[dict], label: str) -> dict:
    """Fold several script reports into one column.

    Check names are prefixed with their script, because two scripts can
    legitimately use the same wording for different checks and collapsing them
    in the matrix would compare unrelated things.
    """
    merged: dict = {
        "label": label,
        "results": [],
        "notes": [],
        "context": {},
        # Per-script wall clock, kept so the page can say where a run's time
        # goes. It is recorded per report and was being dropped here.
        "elapsed": {},
    }
    for rep in reports:
        script = rep.get("script", "?")
        if rep.get("elapsed") is not None:
            merged["elapsed"][script] = rep["elapsed"]
        if not merged["context"]:
            merged["context"] = dict(rep.get("context", {}))
        for result in rep.get("results", []):
            entry = dict(result)
            entry["name"] = f"{script}: {result['name']}"
            # Match on the key, display the full name. A check whose message
            # carried its measurements would otherwise split into one row per
            # backend, each showing a gap where the others answered.
            entry["key"] = f"{script}: {result.get('key', result['name'])}"
            merged["results"].append(entry)
        merged["notes"].extend(rep.get("notes", []))
    return merged


def status(column: dict) -> str:
    """A column's status, defaulting to `ok` for one that carries results."""
    declared = column.get("status")
    if declared in STATUSES:
        return declared
    return "ok" if column.get("results") else "not-run"


def label(column: dict) -> str:
    return column.get("label") or column.get("context", {}).get("backend", "?")


def display_labels(columns: list[dict]) -> list[str]:
    """Labels, with the platform added wherever one alone is ambiguous.

    pyvisa-py on Linux and pyvisa-py on Windows are two columns with one name.
    Prose that lists them has to tell them apart -- "PyVISA-py, PyVISA-py
    produced no results" names neither.
    """
    names = [label(c) for c in columns]
    return [
        f"{n} ({c.get('os_label')})" if names.count(n) > 1 and c.get("os_label") else n
        for n, c in zip(names, columns)
    ]


@dataclasses.dataclass
class Row:
    """One check, across every column."""

    key: str
    name: str
    rule: str
    script: str
    #: The full result per column, positionally, or None where that column has
    #: no answer for this check.
    cells: list[dict | None]
    #: Which columns were compared to reach `differs` -- the `ok` ones.
    compared: list[int]
    #: Where the check is written, as `checks/03_srq.py:57`, from whichever
    #: column recorded one. The same check lives at the same line whoever ran
    #: it, so the first answer is as good as any.
    source: str = ""

    @property
    def file(self) -> str:
        """`checks/01_smoke.py`, without the line number."""
        return self.source.partition(":")[0]

    @property
    def line(self) -> str:
        return self.source.partition(":")[2]

    @property
    def label(self) -> str:
        """What to call this row in a matrix.

        Not `name`. Names used to carry their evidence -- "a read of a
        complete message returns VI_SUCCESS, got <StatusCode.success: 0>" --
        and the union takes each row's name from the first column that has it,
        which is pyvisa-py. So the row header was showing one implementation's
        measurement as though it were the check's identity, next to columns
        that may have answered something else entirely.

        The key is the check's identity, which is why it is what the columns
        are joined on. Show that, and let the per-column detail carry what each
        one actually returned.
        """
        _, _, rest = self.key.partition(": ")
        return rest or self.key

    @property
    def outcomes(self) -> list[str]:
        return [c["outcome"] if c else "-" for c in self.cells]

    @property
    def compared_outcomes(self) -> list[str]:
        return [self.outcomes[i] for i in self.compared]

    @property
    def differs(self) -> bool:
        """Whether the implementations disagree about this check.

        Only `ok` columns count. A leg that never ran would otherwise turn one
        dead runner into a full page of fabricated disparities -- the loudest
        possible way for the matrix to lie.

        A `-` in an `ok` column does count: a script that crashed under one
        implementation and not another is a real difference, and reporting it
        as agreement is how a whole missing script reads as "not applicable".
        """
        return len(set(self.compared_outcomes)) > 1

    @property
    def all_skipped(self) -> bool:
        return bool(self.compared) and set(self.compared_outcomes) == {"SKIP"}


@dataclasses.dataclass
class Matrix:
    columns: list[dict]
    rows: list[Row]

    @property
    def compared(self) -> list[int]:
        return [i for i, c in enumerate(self.columns) if status(c) == "ok"]

    @property
    def disagreements(self) -> list[Row]:
        return [r for r in self.rows if r.differs]

    @property
    def all_skipped(self) -> list[Row]:
        return [r for r in self.rows if r.all_skipped]

    def unique_failures_by_column(self) -> "collections.OrderedDict[int, list[Row]]":
        """Per column, the rows only that column fails.

        The page used to ask this of pyvisa-py alone, which made the whole
        matrix an argument about one implementation. It is the same question
        for every column, and asking it of all of them is what turns the page
        from "how is pyvisa-py doing" into "how do these four compare" -- which
        is what a conformance matrix is for. A vendor failing something the
        other three pass is exactly as interesting, and until now the page did
        not say it anywhere.
        """
        found: "collections.OrderedDict[int, list[Row]]" = collections.OrderedDict()
        for i in self.compared:
            others = [j for j in self.compared if j != i]
            if not others:
                continue
            rows = [
                r
                for r in self.rows
                if r.outcomes[i] == "FAIL"
                and all(r.outcomes[j] == "PASS" for j in others)
            ]
            if rows:
                found[i] = rows
        return found

    def shared_failures(self) -> list[Row]:
        """Rows every compared column fails.

        Not a comparison between implementations but a statement about the
        check: four independent implementations agreeing on a failure usually
        means the suite is asserting something the spec does not require, or
        the mock is wrong. Worth its own list for exactly that reason -- and it
        is how the `HiSLIPConnectionLost` bug was found.
        """
        if len(self.compared) < 2:
            return []
        return [
            r
            for r in self.rows
            if all(r.outcomes[i] == "FAIL" for i in self.compared)
        ]

    def counts(self, index: int) -> collections.Counter:
        column = self.columns[index]
        return collections.Counter(r["outcome"] for r in column.get("results", []))

    def by_script(self) -> "collections.OrderedDict[str, list[Row]]":
        """Rows grouped by the file they live in.

        By file rather than by the script's display name: the name is prose
        ("smoke (hislip)") and the file is what you open.
        """
        groups: collections.OrderedDict[str, list[Row]] = collections.OrderedDict()
        for row in self.rows:
            groups.setdefault(row.file or row.script, []).append(row)
        return groups


def build(columns: list[dict]) -> Matrix:
    """Line columns up into rows, in the order the checks were written.

    The union of keys keeps the order of the first column that has each, so the
    table reads in source order rather than in the order of whichever
    implementation happened to answer.
    """
    lookups = [
        {r.get("key", r["name"]): r for r in c.get("results", [])} for c in columns
    ]
    compared = [i for i, c in enumerate(columns) if status(c) == "ok"]

    rows: list[Row] = []
    seen: set[str] = set()
    for column in columns:
        for result in column.get("results", []):
            key = result.get("key", result["name"])
            if key in seen:
                continue
            seen.add(key)
            name = result["name"]
            script, _, rest = name.partition(": ")
            cells = [lk.get(key) for lk in lookups]
            rule = next((c["rule"] for c in cells if c and c.get("rule")), "")
            source = next((c["source"] for c in cells if c and c.get("source")), "")
            rows.append(
                Row(
                    key=key,
                    name=name,
                    rule=rule,
                    script=script if rest else "",
                    source=source,
                    cells=cells,
                    compared=compared,
                )
            )
    return Matrix(columns=columns, rows=rows)


# -- markdown ---------------------------------------------------------------
#
# Rendered for $GITHUB_STEP_SUMMARY, which is read on a phone as often as not.
# Failures and disagreements come first and the full grid comes last, folded
# away: a report is read to find out what went wrong, and forty green rows
# above the one red one buries its own point.

#: Outcomes as they appear in a cell. FAIL is the only one shouted, because it
#: is the only one worth finding by eye in a wall of table.
_CELL = {"PASS": "pass", "FAIL": "**FAIL**", "SKIP": "skip", "-": "&mdash;"}


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    return (
        [f"| {' | '.join(header)} |", f"|{'|'.join(['---'] * len(header))}|"]
        + [f"| {' | '.join(r)} |" for r in rows]
    )


def _outcome_rows(matrix: Matrix, rows: list[Row]) -> list[list[str]]:
    return [
        [_md_escape(r.label)]
        + [_CELL.get(o, o) for o in (r.outcomes[i] for i in matrix.compared)]
        + [_md_escape(r.rule) or ""]
        for r in rows
    ]


def render_markdown(
    matrix: Matrix,
    protocol: str,
    *,
    max_rows: int = 60,
) -> str:
    """One transport's results as GitHub-flavoured Markdown."""
    out: list[str] = [f"## {protocol}", ""]

    # -- what each column did, including the ones that did nothing ----------
    shown = display_labels(matrix.columns)
    # The OS column earns its place only when the columns differ about it.
    show_os = len({c.get("os_label", "") for c in matrix.columns if c.get("os_label")}) > 1
    counts = []
    for i, column in enumerate(matrix.columns):
        st = status(column)
        n = matrix.counts(i)
        cells = (
            [str(n.get(k, 0)) for k in ("PASS", "FAIL", "SKIP")]
            if st == "ok"
            else ["&mdash;"] * 3
        )
        if st != "ok":
            note = f"**{st}** &mdash; {column.get('reason', '')}"
        elif column.get("errors"):
            # "ok" alone would be a lie here: the column is trustworthy but
            # incomplete, and the checks the crashed scripts would have run are
            # absent rather than passing.
            note = (
                f"ok, but **{len(column['errors'])} script(s) crashed** and "
                f"their checks are absent: "
                + ", ".join(f"`{e}`" for e in column["errors"])
            )
        else:
            note = "ok"
        row = [_md_escape(label(column))]
        if show_os:
            row.append(column.get("os_label", ""))
        counts.append(row + cells + [_md_escape(note)])
    header = ["Column"] + (["OS"] if show_os else []) + [
        "Pass", "Fail", "Skip", "Status"
    ]
    out += _md_table(header, counts) + [""]

    missing = [c for c in matrix.columns if status(c) != "ok"]
    if missing:
        # Stated as prose as well as in the table. A comparison with a column
        # silently absent reads like agreement between the ones that remain,
        # and the table alone is easy to skim past.
        out += [
            f"> {len(missing)} of {len(matrix.columns)} implementations produced "
            f"no results, so every row below compares only the rest.",
            "",
        ]

    headers = ["Check"] + [
        _md_escape(shown[i]) for i in matrix.compared
    ] + ["Rule"]

    def section(title: str, rows: list[Row], blurb: str = "") -> None:
        if not rows:
            return
        out.append(f"### {title} ({len(rows)})")
        if blurb:
            out.extend(["", blurb])
        out.append("")
        shown = rows[:max_rows]
        out.extend(_md_table(headers, _outcome_rows(matrix, shown)))
        if len(rows) > len(shown):
            out.extend(["", f"_…and {len(rows) - len(shown)} more._"])
        out.append("")

    # Once per implementation. The Markdown is what gets read on a phone, so
    # it should carry the same shape as the page: four columns compared, not
    # one prosecuted.
    by_column = matrix.unique_failures_by_column()
    unique_keys = {r.key for rows in by_column.values() for r in rows}
    for index, rows in by_column.items():
        name = label(matrix.columns[index])
        section(
            f"Failures unique to {name}",
            rows,
            f"Checks {name} fails, on every platform it ran on, that every "
            "other implementation here passes.",
        )
    shared = matrix.shared_failures()
    section(
        "Failures shared by every implementation",
        shared,
        "Four independent implementations agreeing on a failure usually means "
        "the check asserts more than the clause requires, or the mock is "
        "wrong. Questions about the suite before questions about the "
        "libraries.",
    )
    section(
        "Where the implementations disagree",
        [
            r
            for r in matrix.disagreements
            if r.key not in unique_keys and r not in shared
        ],
    )

    skipped = matrix.all_skipped
    if skipped:
        out += [
            f"### Skipped everywhere ({len(skipped)})",
            "",
            "A skipped check is not a passing one. These are the ones to watch: "
            "the ones that stay skipped run after run are how a gap in coverage "
            "hides in plain sight.",
            "",
        ] + _md_table(headers, _outcome_rows(matrix, skipped[:max_rows])) + [""]

    out += [
        "<details><summary>Full matrix "
        f"({len(matrix.rows)} checks)</summary>",
        "",
    ]
    for script, rows in matrix.by_script().items():
        out += [f"**{_md_escape(script) or 'checks'}**", ""]
        out += _md_table(headers, _outcome_rows(matrix, rows)) + [""]
    out += ["</details>", ""]
    return "\n".join(out)
