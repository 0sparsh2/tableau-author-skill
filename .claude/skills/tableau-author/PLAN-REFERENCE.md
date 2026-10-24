# Plan format

The machine-readable schema is `PLAN_SCHEMA` in `twbgen/chartspec.py`. Every key
below is required; use `[]` or `null` when unused.

## Contents
- Top level
- Data model (several CSVs)
- Charts and fields
- Chart types
- Filters, sort, reference lines
- Parameters
- Calculations and formats
- Dashboard
- Full examples

## Top level

```json
{ "name": "Workbook name", "data_model": null, "parameters": [], "calculations": [], "formats": [],
  "charts": [], "dashboard": null }
```

## Data model (several CSVs)

```json
"data_model": { "links": [ { "kind": "relationship", "left": "orders.csv", "right": "customers.csv",
                              "left_field": "Customer ID", "right_field": "Customer ID (customers.csv)",
                              "join_type": null } ] }
```

- One link per table after the first, each linking a table already in the model
  to a new one, with one key pair.
- `kind`: `relationship` (default, Tableau's logical layer) or `join` with
  `join_type` `inner` or `left`. Don't mix kinds.
- Field names follow PROFILE: a column that an earlier table already has is
  `<column> (<file>.csv)`.

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
- `role`: `x`, `y`, `color`, `size`, `label`, `detail`, `tooltip`, and `geo` for
  map locations. Put numbers a reader would hover for on `tooltip`.

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
| filledmap | no x/y; a geographic field (PROFILE `geo`: country/state/county/zip) on `geo`; a measure on color |
| symbolmap | no x/y; geographic field(s) on `geo`, coarse to fine (State, City); a measure on size |
| pointmap | longitude on x, latitude on y (agg `auto` = average); the entity on detail |

One axis can't mix dimensions and measures.

## Filters, sort, reference lines

```json
"filters": [ { "field": "Region", "agg": "auto", "include": ["East", "West"], "exclude": null, "min": null, "max": null,
               "top": null, "by_field": null, "by_agg": null },
             { "field": "Sales",  "agg": "sum",  "include": null, "exclude": null, "min": 100, "max": null,
               "top": null, "by_field": null, "by_agg": null },
             { "field": "Customer Name", "agg": "auto", "include": null, "exclude": null, "min": null, "max": null,
               "top": "Top N", "by_field": "Sales", "by_agg": "sum" } ],
"sort": { "field": "Region", "by_field": "Sales", "by_agg": "sum", "direction": "desc" },
"reference_lines": [ { "field": "Sales", "agg": "sum", "formula": "average" } ]
```

- Value lists (`include`/`exclude`) filter dimensions; `min`/`max` filter measures.
  Values must come from PROFILE samples.
- Top N: `top` (a number, or an integer parameter's name) + `by_field`/`by_agg`
  keeps a dimension's highest members by that measure.
- Sort: a dimension ordered by a measure; both must be fields of the chart
  (add the measure to `tooltip` if it isn't shown).
- Reference line: `average`, `median`, `min` or `max` of a measure on x or y.

## Parameters

```json
"parameters": [ { "name": "Top N", "datatype": "integer", "value": 10, "allowed": null, "min": 3, "max": 25, "step": 1 },
                { "name": "Selected State", "datatype": "string", "value": "California",
                  "allowed": null, "min": null, "max": null, "step": null } ]
```

- `allowed` → dropdown; `min`/`max`/`step` → slider (integer, real, date); neither → type-in.
- Use in calculations as `[Top N]` (or `[Parameters].[Top N]`) and in Top-N filters.
- Show a control by listing the name in the dashboard's `parameters`.

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
               "filters": ["Category"], "parameters": ["Top N"],
               "actions": [ { "type": "filter", "source": "Which regions lead in sales?", "targets": null,
                              "field": "Region", "parameter": null, "on": null } ],
               "orientation": "vertical" }
```

- Only with two or more related charts. `charts` lists chart titles in order.
- `filters`: 1-3 dimensions; each becomes a dropdown that filters every chart.
- `parameters`: controls to show (slider, dropdown or type-in, from the parameter).
- `actions`, each from a `source` chart and a non-date dimension shown on it
  (`field`, or null for the first one):
  - `filter`: clicking marks filters `targets` (null = all other charts);
  - `highlight`: `on: "hover"` or click highlights the field across charts;
  - `parameter`: clicking sets `parameter` (same datatype as the field).

## Full examples

- `twbgen/examples/plans/feature_tour.json`: combo, bar with sort and a reference
  line, histogram, dualline, formats, a calculation, dashboard title and filters.
- `twbgen/examples/plans/customer_geography.json` (with `multi/orders.csv` and
  `multi/customers.csv`): two related tables, filled and symbol maps, a Top-N
  parameter slider, and filter, highlight and parameter actions.
- `twbgen/examples/plans/store_points.json` (with `multi/stores.csv`): a point map.
