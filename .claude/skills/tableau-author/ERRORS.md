# Build errors and fixes

`author.py` prints every problem at once, prefixed with where it is (`chart 'X'`,
`calculation 'Y'`, `dashboard`, `formats`). Fix all of them, then rebuild.

| Message contains | Fix |
|---|---|
| `unknown field 'X'; did you mean [...]` | Use the suggested PROFILE name exactly (case and spaces matter). |
| `a <chart> chart needs ...` | Move fields to the roles the message lists, or pick a chart type that fits the fields. |
| `role 'x' mixes dimensions [...] and measures [...]` | Put dimensions on one axis and measures on the other; extra dimensions can go to color or detail. |
| `date field 'X' needs a date part` | Set `agg` to year/quarter/month/week/day (or count/countd). |
| `'avg' needs a numeric field` | Use count or countd for text fields. |
| `'month' only applies to date fields` | Use a measure aggregation, or pick the date field. |
| `is an aggregate calculation; use agg 'auto'` | Set `agg: "auto"` for that calculation. |
| `include/exclude lists need a dimension` | Use `min`/`max` for measures, or filter a dimension. |
| `min/max needs a measure` | Use `include`/`exclude` values for dimensions. |
| `needs include/exclude values or min/max` | Fill one of them, or drop the filter. |
| `sort: ... must be fields of the chart` | Add the sort measure to the chart (tooltip is fine). |
| `sort: 'field' must be a dimension and 'by_field' a measure` | Swap them or pick other fields. |
| `reference line on ...: needs a measure that is on the chart's x or y` | Point it at an axis measure. |
| `references unknown field [X]` (calculation) | Fix the bracketed name to a PROFILE field or another calculation's name. |
| `unknown function F()` | Use a Tableau function (SUM, AVG, IF/THEN/ELSE/END, DATEDIFF, ZN, IIF...). |
| `unbalanced parentheses or brackets` | Close every `(` and `[`. |
| `name collides with an existing field` | Rename the calculation. |
| `chart titles must be unique` | Rename duplicates. |
| `dashboard: unknown chart titles` | Use exact chart titles from `charts`. |
| `filter 'X' must be a dimension` (dashboard) | Dashboard dropdowns take dimensions only. |
| `'X' is a dimension; number formats apply to measures` | Remove that format entry. |
| `compiler: ...` | Usually a plan inconsistency the planner missed; read it, adjust the plan. |
| `integrity: ...` or `tableau-desktop-schema: ...` | Generator bug, not a plan mistake. Stop and report it with the plan file. |
