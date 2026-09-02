# SPDX-License-Identifier: GPL-3.0-or-later
"""Folding runs into columns, and columns into a matrix.

Four places grew their own copy of the same join -- `compare.py`, both tools in
`tools/`, and `report.render_matrix` -- and they had already drifted in the
details that matter: whether a column that produced nothing appears at all, and
whether a row where one implementation is simply absent counts as a
disagreement. Those are not rendering details. They decide what the published
page claims.

The join itself is on `harness.stable_key`, never on the displayed name. Check
names carry their measurements, which is right for reading one run and wrong
for lining several up: two backends reporting different status codes produce
two different names, so the row splits in two and each column shows a gap where
the other one answered.
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
    merged: dict = {"label": label, "results": [], "notes": [], "context": {}}
    for rep in reports:
        script = rep.get("script", "?")
        if not merged["context"]:
            merged["context"] = dict(rep.get("context", {}))
        for result in rep.get("results", []):
            entry = dict(result)
            entry["name"] = f"{script}: {result['name']}"
            # Match on the masked key, display the full name. A check whose
            # message carries its measurements would otherwise split into one
            # row per backend, each showing a gap where the others answered.
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
    def label(self) -> str:
        """What to call this row in a matrix.

        Not `name`. Check names carry their evidence -- "a read of a complete
        message returns VI_SUCCESS, got <StatusCode.success: 0>" -- and the
        union takes each row's name from the first column that has it, which is
        pyvisa-py. So the row header was showing one implementation's
        measurement as though it were the check's identity, next to columns
        that may have answered something else entirely.

        The masked key is the check's identity, which is why it is what the
        columns are joined on. Show that, and let the per-column detail carry
        what each one actually returned.

        The `*` that stable_key leaves behind is shown as it is. Prettifying it
        into an ellipsis looked better until `*IDN?` came out as `…IDN?` -- the
        mask and a SCPI command star are the same character, and nothing in the
        string says which is which.
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

    def subject_columns(self, subject: str = "py") -> list[int]:
        """Which compared columns are the implementation under test.

        More than one column can be the same implementation -- pyvisa-py on
        Linux and on Windows is the useful case, because it gives every
        cross-OS disagreement a same-OS control. They are one subject, so
        `backend` is matched before the display label, which is identical for
        both.
        """
        by_backend = [
            i for i in self.compared if self.columns[i].get("backend") == subject
        ]
        if by_backend:
            return by_backend
        return [i for i in self.compared if label(self.columns[i]) == subject]

    def unique_failures(self, subject: str = "py") -> list[Row]:
        """Rows the subject fails everywhere and every other column passes.

        This used to be spelled `len(outcomes) == 3 and outcomes == [FAIL,
        PASS, PASS]`, which silently counted nothing as soon as a fourth
        implementation joined the matrix, and counted the wrong thing if the
        columns were ever reordered.

        Every subject column has to fail. A check that fails under pyvisa-py on
        Linux and passes under pyvisa-py on Windows is telling you about the
        platform, not about the library, and it does not belong in a list
        headed "confirmed findings".
        """
        subjects = self.subject_columns(subject)
        others = [i for i in self.compared if i not in subjects]
        if not subjects or not others:
            return []
        return [
            r
            for r in self.rows
            if all(r.outcomes[i] == "FAIL" for i in subjects)
            and all(r.outcomes[i] == "PASS" for i in others)
        ]

    def counts(self, index: int) -> collections.Counter:
        column = self.columns[index]
        return collections.Counter(r["outcome"] for r in column.get("results", []))

    def by_script(self) -> "collections.OrderedDict[str, list[Row]]":
        groups: collections.OrderedDict[str, list[Row]] = collections.OrderedDict()
        for row in self.rows:
            groups.setdefault(row.script, []).append(row)
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
    subject: str = "py",
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

    unique = matrix.unique_failures(subject)
    unique_keys = {r.key for r in unique}
    subject_name = next(
        (label(matrix.columns[i]) for i in matrix.subject_columns(subject)), subject
    )
    section(
        f"Failures unique to {subject_name}",
        unique,
        f"Checks {subject_name} fails, on every platform it ran on, that every "
        "other implementation here passes.",
    )
    section(
        "Where the implementations disagree",
        [r for r in matrix.disagreements if r.key not in unique_keys],
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
