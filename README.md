# tableau-author

A Claude Code skill and a small Python toolkit that turn a CSV and a plain-language
request into a Tableau workbook (`.twbx`), and review or improve existing workbooks.

Claude writes a compact JSON **plan** (charts, fields, filters, dashboard); the
toolkit places fields on Tableau's shelves, writes the workbook XML, and validates
it the way Tableau Desktop does before you ever open it.

```text
"Which regions lead in sales, and how is profit trending? Make a dashboard I can filter by category."
        │  Claude (the skill) writes a plan
        ▼
plan.json ──► chartspec.py (field placement) ──► spec_to_twb.py (workbook XML)
                                                     │
                     integrity.py + desktop_check.py ◄┘   errors go back to Claude (≤3 repairs)
                                                     │
                                                     ▼
                                              workbook.twbx  →  open in Tableau
```

## Use it in Claude Code

Clone the repo and open it in Claude Code; the skill lives in
`.claude/skills/tableau-author/` and is picked up automatically. Then ask, e.g.:

- "Build a Tableau dashboard from twbgen/examples/sales.csv showing sales by region and the monthly trend."
- "Review twbgen/out/plans/feature_tour.twbx and tell me what to improve."

No API key is needed: Claude Code itself writes the plan. To use the skill in another
project, copy `.claude/skills/tableau-author/` and `twbgen/` into it.

## Use the toolkit directly

Requirements: Python 3.10+ (standard library only) and `xmllint` (preinstalled on macOS;
`libxml2-utils` on Debian/Ubuntu).

```bash
python3 twbgen/author.py --csv twbgen/examples/sales.csv --plan twbgen/examples/plans/feature_tour.json -o out/feature_tour.twbx
```

```bash
python3 twbgen/selftest.py
```

Other tools: `profile.py` (data profile for the model), `describe.py` (any workbook →
plan-like summary), `lint.py` (best-practice findings), `integrity.py` (broken
references), `normalize.py` (resolve forward-compatibility wrappers).

`author.py --request "…"` calls the Claude API instead of using a plan file
(structured outputs, prompt caching, repair loop); it needs `pip install anthropic`
and `ANTHROPIC_API_KEY`.

## How workbooks are validated

1. **Placement checks** (`chartspec.py`): every chart type's field requirements, with
   errors written for a model to act on ("unknown field 'Regoin'; did you mean ['Region']?").
2. **Integrity** (`integrity.py`): every shelf, encoding, filter, sort and reference-line
   field resolves; filters have slices; sheets, windows and dashboard zones correspond.
   No false positives on 32 Tableau-saved workbooks.
3. **Tableau Desktop's own schema** (`desktop_check.py`): Desktop doesn't validate
   against the published XSD; it preprocesses its internal schema with feature flags
   taken from the workbook's manifest. `desktop_check.py --extract-from <Tableau .app>`
   reads that schema from **your local installation** (cached in
   `twbgen/xsd/desktop-local/`, git-ignored, never redistributed) and reproduces the check.
4. The published schema from
   [tableau/tableau-document-schemas](https://github.com/tableau/tableau-document-schemas)
   (Apache-2.0, in `twbgen/xsd/`) is used by `validate.py` for analysis.

Output is checked by eye in Tableau Desktop 2026.3.

## Supported

- Charts: bar, stacked bar, line, area, combo (bars + line, dual axis), dual line,
  scatter, histogram, text/KPI table, highlight table, pie, treemap.
- Maps: filled (country/state/county/zip), symbol (state/city), and point maps
  from latitude/longitude.
- Several CSVs, linked by relationships or by inner/left joins.
- Parameters (dropdown, slider, type-in) used in calculations and Top-N filters.
- Calculations, categorical, range and Top-N filters, sorts, reference lines,
  currency/percent/number formats.
- One dashboard per workbook: title, dropdown filters applied to every chart,
  parameter controls, and filter / highlight / parameter actions.

## Acknowledgements

- XML structures for several features were learned from Tableau-saved reference
  workbooks in [laviDrori0702/tableau-dashboard-creator-skill](https://github.com/laviDrori0702/tableau-dashboard-creator-skill) (MIT).
- Official TWB schemas: [tableau/tableau-document-schemas](https://github.com/tableau/tableau-document-schemas) (Apache-2.0).

Not affiliated with or endorsed by Tableau or Salesforce. Tableau is a trademark of Salesforce, Inc.

## License

MIT; see [LICENSE](LICENSE). `twbgen/xsd/twb_2026.2.0.xsd` is Apache-2.0
(see `twbgen/xsd/LICENSE-tableau-document-schemas.txt`); `twb_2026.2.0.local.xsd` is that
file with its two imports pointed at local stubs.
