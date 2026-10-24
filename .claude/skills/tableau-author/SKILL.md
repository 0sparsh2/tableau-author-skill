---
name: tableau-author
description: Builds Tableau workbooks (.twbx) from a CSV and a plain-language request, and reviews or improves existing .twb/.twbx workbooks, using this project's twbgen compiler and Tableau Desktop's own schema checks. Use when the user asks to create a Tableau chart, viz, dashboard or workbook from data, or to review, lint, check or improve a Tableau workbook.
---

# Tableau author

You write a small JSON **plan** (charts, fields, filters, dashboard); the
tools in `twbgen/` turn it into a workbook and validate it the way Tableau
Desktop does. Never write TWB XML by hand.

All commands run from the project root with plain `python3` (stdlib only;
`xmllint` must be on PATH, as it is on macOS).

## Before the first build on a machine

If `twbgen/xsd/desktop-local/` doesn't exist and Tableau Desktop is
installed, cache Desktop's schema once (without it, `author.py` warns and
skips the Desktop check):

```bash
python3 twbgen/desktop_check.py --extract-from "$(ls -d /Applications/Tableau*.app | tail -1)"
```

## Create a workbook

1. **Profile the data.** `python3 twbgen/profile.py path/to/data.csv` gives exact
   field names, types, roles, sample values, ranges and geographic roles. For
   several related CSVs, pass them all (`profile.py orders.csv customers.csv`);
   the plan then links them in `data_model`. Only CSV sources are supported; ask
   for CSV exports otherwise.
2. **Clarify only what changes the chart:** which date or measure when
   several fit, or the time grain. Otherwise decide and say what you chose.
3. **Write the plan** to `<out-dir>/<name>.plan.json`, following
   [PLAN-REFERENCE.md](PLAN-REFERENCE.md). Use PROFILE's exact field names and
   the fewest charts that answer the question.
4. **Build and check:**
   ```bash
   python3 twbgen/author.py --csv path/to/data.csv --plan <out-dir>/<name>.plan.json -o <out-dir>/<name>.twbx
   ```
   Repeat `--csv` for each table, in the order the plan's `data_model` expects.
   This places fields on shelves, compiles, runs `integrity.py` and Desktop's
   schema check. It prints the output path on success, or `FAILED:` with every
   problem.
5. **Repair on failure.** Fix every listed problem in the plan (see
   [ERRORS.md](ERRORS.md)) and rerun step 4. Stop after 3 repairs and show the
   user the remaining errors. Errors prefixed `integrity:` or
   `tableau-desktop-schema:` are generator bugs, not plan mistakes: report them.
6. **Open it** if the user wants to look:
   `open -a "$(ls -d /Applications/Tableau*.app | tail -1)" <out-dir>/<name>.twbx`.
   You can't see Tableau, so ask the user to confirm each chart renders.
7. **Report** each chart's question and how it answers it, the dashboard
   filters, and the files written (`.twbx`, `.plan.json`, `.author.json` log).

Default `<out-dir>` is `twbgen/out/` unless the user names one. Never overwrite
an existing workbook without asking.

## Improve an existing workbook

See [IMPROVE.md](IMPROVE.md): run `lint.py`, `integrity.py` and `describe.py` on
the `.twb`/`.twbx`, explain the findings, and rebuild an improved copy when
the workbook is CSV-backed.

## What the generator supports

- Charts: bar, stackedbar, line, area, combo (bars + line, dual axis), dualline,
  scatter, histogram, text/KPI table, highlight table, pie, treemap, and maps:
  filledmap, symbolmap (geographic fields) and pointmap (latitude/longitude).
- Several CSVs linked by relationships (default) or inner/left joins.
- Parameters (dropdown, slider or type-in), used in calculations and Top-N filters.
- Calculations (Tableau syntax), categorical, range and Top-N filters, sorts,
  reference lines (average/median/min/max), currency/percent/number formats.
- One dashboard per workbook: title, dropdown filters for every chart,
  parameter controls, and filter / highlight / parameter actions.
- Not yet: LOD-specific UI, table calculations on shelves, date-range filters,
  multi-key links, actions on more than one field, formatting beyond number
  formats. Say so instead of approximating.

## Example

Request: "Which regions lead in sales, and how is profit trending?" on
`twbgen/examples/sales.csv`. A working plan for a similar request is
`twbgen/examples/plans/regional_overview.json`; every file in that folder is
a tested plan.
