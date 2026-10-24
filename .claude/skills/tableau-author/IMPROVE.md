# Improve an existing workbook

Works on `.twb` or `.twbx`, from any Tableau version (2020+ tested).

1. **Read it:**
   ```bash
   python3 twbgen/describe.py path/to/workbook.twbx
   ```
   This gives a compact summary in the same vocabulary as a plan: sheets with
   rows/cols pills and the shelf operator, marks, encodings, filters, calculations,
   and dashboards.
2. **Check it:**
   ```bash
   python3 twbgen/lint.py path/to/workbook.twbx
   ```
   ```bash
   python3 twbgen/integrity.py path/to/workbook.twbx
   ```
   - `lint.py` returns findings with a rule, target, message and suggestion:
     default names, pies, empty or orphan sheets, unused or copied calcs, stacked
     measures, single-mark views, missing tooltips, automatic marks, busy dashboards.
   - `integrity.py` reports broken references.
   - If the Desktop schema is cached and the file is a `.twb`, also run
     `python3 twbgen/desktop_check.py path/to/workbook.twb`.
3. **Explain** the findings in order of impact: what is wrong, why it matters to a
   reader, and the concrete change. Lint suggestions are advisory. For example, a
   scatterplot matrix legitimately stacks many measures. Judge each one against
   what the sheet is for.
4. **Rebuild an improved copy only when the workbook is CSV-backed** (the
   describe output shows connection `federated` over a CSV) and the CSV is
   available:
   - Translate each sheet into a plan chart, keeping sheet titles the user
     didn't ask to change, and apply the fixes.
   - Build with `author.py` to a new file such as `<name>.improved.twbx`.
     Never overwrite the original.
   - List what changed, sheet by sheet.
5. **Otherwise**, give the user the change list to make in Tableau (extract,
   database, published and multi-table sources can't be regenerated yet).
