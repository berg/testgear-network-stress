# Working in this repository

## Citing the specs

Every check cites the clause it depends on: `@check(..., rule="...")`. Prose in docstrings, comments, skip messages, assertion text and docs/ follows the same rules. A failed check that cites a clause reads as a bug report; one with no citation is just an opinion.

Read the clause in the spec before you cite it. Never cite from memory.

### Always say what kind of reference it is
VPP-4.3 and VXI-11 number RULE, OBSERVATION, RECOMMENDATION and PERMISSION on separate counters, and section headings on another. So "VPP-4.3 3.7.12" on its own can mean up to four unrelated things. Always state the kind:

- `VPP-4.3 RULE 6.1.3`, `VXI-11 RULE B.6.23`, `VPP-4.3 OBSERVATION 3.7.12`
- `VPP-4.3 §6.1.1`, `VXI-11 §B.6.8`, `IVI-6.1 §6.7` for a **section**. Cite the section only when the requirement is in prose or a table (such as an error-code table) and not in a numbered statement.
- Keep lettered sub-rules intact: `RULE 5.1.31-a`.
- Use this form everywhere: `rule=`, docstrings, comments, skip and assert messages, README and docs/.

### Cite the clause that actually says it
- Cite the specific numbered statement, not the section heading it sits under. For example, `§3.6.2.1` is the viLock heading; the requirements are RULE 3.6.24, 3.6.18 and so on.
- Neighbouring rules are easy to mix up: 6.1.2 (TERM_CHAR) vs 6.1.3 (MAX_CNT), 3.6.29 (no prior lock) vs 3.6.30 (nested shared lock). Re-read the text of the rule you're citing.
- Check scope. A rule written for one resource or transport (HiSLIP, SOCKET, a specific INSTR) doesn't apply to another.
- Don't over-attribute. Cite a clause only for what it actually establishes. RULE B.6.72 says a link can't re-lock a lock it already holds. It does **not** say VXI-11 locks are exclusive. That follows from Device_LockParms having no lock-type field.
- Status codes must exist in the spec. Check the operation's error table, and never invent one (`VI_ERROR_TSK_TIMEOUT` isn't a VISA code).
- Unheaded descriptive text isn't normative. Don't cite it as a rule.

### If nothing says so, don't cite anything
Don't point a citation at the nearest plausible clause. If no clause mandates the behaviour, leave `rule=` out and say so in the docstring. The check can still run, but it shouldn't claim the spec requires it.

### When a citation changes, fix every place it appears
A changed citation has to change everywhere: `rule=`, the docstring next to it, skip and assert messages, README examples, `docs/`. Fixing only the decorator leaves the file contradicting itself, which is worse than a consistent mistake.

### Verify
- `tools/spec_rules.py --specs <dir> --out docs/spec-coverage.md` matches `rule=` against the normative statements it extracts. A citation that points at a heading or at a number that doesn't exist covers nothing.
- For a citation audit, fan out agents to check each reference, then have a second agent that hasn't seen the first agent's reasoning re-derive each finding. In the last audit that second pass caught a fabricated verification and two proposed "fixes" that were themselves wrong.
