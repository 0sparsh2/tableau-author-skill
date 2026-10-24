# Plan format

The machine-readable schema is `PLAN_SCHEMA` in `twbgen/chartspec.py`. Every key
below is required; use `[]` or `null` when unused.

## Contents
- Top level
- Charts and fields
- Chart types
- Filters, sort, reference lines
- Calculations and formats
- Dashboard
- Full example

## Top level

```json
{ "name": "Workbook name", "calculations": [], "formats": [], "charts": [], "dashboard": null }
```

## Charts and fields

```json
{ "title": "Which regions lead in sales?", "chart": "bar",
  "fields": [ { "field": "Region", "agg": "auto", "continuous_date": null, "role": "y" },
              { "field": "Sales",  "agg": "sum",  "continuous_date": null, "role": "x" } ],
  "filters": [], "sort": null, "reference_lines": [] }
```

- `title`: the question the chart answers; becomes the sheet name. Unique.
- `field`: exact PROFILE name, or a calculation's name.
- `agg`: dimensions `auto`; measures `sum` (default via `auto`), `avg`, `min`,
  `max`, `median`, `count`, `countd`; dates `year`, `quarter`, `month`, `week`, `day`.
- `continuous_date`: `true` for a timeline (trends), `false` for date parts as
  categories, `null` for the chart's default (timeline for line/area/combo/dualline).
- `role`: `x`, `y`, `color`, `size`, `label`, `detail`, `tooltip`. Put numbers a
  reader would hover for on `tooltip`.

## Chart types

| chart | needs |
|---|---|
| bar | dimension(s) on one axis, measure(s) on the other; prefer dimension on y, measure on x, sorted desc |
| stackedbar | as bar, plus a dimension on color |
| line / area | date on x, measure(s) on y |
| combo | date or dimension on x, exactly two measures on y (bars, then line; synchronized axes) |
| dualline | as combo, both lines |
| histogram | one numeric field on x, nothing else (bin size and record counts are automatic) |
| scatter | measure on x, measure on y, the entity on detail |
| highlight | dimension on x, dimension on y, measure on color (and label) |
| text | dimensions on x/y or none, measure(s) on label; KPIs |
| pie | no x/y, dimension on color, measure on size; only 2-4 slices |
| treemap | no x/y, measure on size, dimension on label/color |

One axis can't mix dimensions and measures.

## Filters, sort, reference lines

```json
"filters": [ { "field": "Region", "agg": "auto", "include": ["East", "West"], "exclude": null, "min": null, "max": null },
             { "field": "Sales",  "agg": "sum",  "include": null, "exclude": null, "min": 100, "max": null } ],
"sort": { "field": "Region", "by_field": "Sales", "by_agg": "sum", "direction": "desc" },
"reference_lines": [ { "field": "Sales", "agg": "sum", "formula": "average" } ]
```

- Value lists (`include`/`exclude`) filter dimensions; `min`/`max` filter measures.
  Values must come from PROFILE samples.
- Sort: a dimension ordered by a measure; both must be fields of the chart
  (add the measure to `tooltip` if it isn't shown).
- Reference line: `average`, `median`, `min` or `max` of a measure on x or y.

## Calculations and formats

```json
"calculations": [ { "name": "Profit Ratio", "formula": "SUM([Profit]) / SUM([Sales])", "datatype": "real" } ],
"formats": [ { "field": "Sales", "format": "currency" }, { "field": "Profit Ratio", "format": "percent" } ]
```

- Only when no existing field answers the question. Tableau syntax, fields as
  `[Field Name]`; a calculation may use another by its name. Use it in charts by
  name with `agg: "auto"`.
- Formats: `currency` (whole dollars), `currency2` (cents), `percent`, `number`,
  `decimal`. Format every money and ratio measure a reader sees.

## Dashboard

```json
"dashboard": { "name": "Overview", "title": "Sales Performance 2024-2025",
               "charts": ["Which regions lead in sales?", "How are sales trending?"],
               "filters": ["Region", "Category"], "orientation": "vertical" }
```

- Only with two or more related charts. `charts` lists chart titles in order.
- `filters`: 1-3 dimensions; each becomes a dropdown that filters every chart.

## Full example

`twbgen/examples/plans/feature_tour.json` uses combo, bar with sort and a
reference line, histogram, dualline, formats, a calculation, and a dashboard
with title and filters. It builds cleanly and renders in Tableau Desktop.
