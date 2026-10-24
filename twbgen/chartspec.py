"""ChartSpec: the LLM-facing plan, compiled to a shelf-level VizSpec.

A chart type plus fields, each with an aggregation and an encoding role,
plus filters, a sort and reference lines. The model never deals with shelves or
the table algebra; `plan_to_vizspec` is our "Show Me" step that places fields
on rows/columns/marks and checks the plan makes a valid chart. All problems
are collected in one pass so a repair turn can fix them together.
"""
from __future__ import annotations

import csv
import difflib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, also under `python -I`
from spec_to_twb import AGG_IN_FORMULA, FORMATS, REFLINE_FORMULAS, load_fields  # noqa: E402

CHARTS = {  # chart -> mark
    "bar": "Bar", "stackedbar": "Bar", "line": "Line", "area": "Area", "scatter": "Circle",
    "text": "Text", "highlight": "Square", "pie": "Pie", "treemap": "Square",
    "combo": "Automatic", "dualline": "Automatic", "histogram": "Bar",
}
RECORDS = "Number of Records"
MEASURE_AGGS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "median": "MEDIAN",
                "count": "COUNT", "countd": "COUNTD"}
DATE_AGGS = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK", "day": "DAY"}
ROLES = ("x", "y", "color", "size", "label", "detail", "tooltip")
FUNCTIONS = set("""ABS ACOS AND ASCII ASIN ATAN ATAN2 ATTR AVG CASE CEILING CHAR CONTAINS COS COT COUNT COUNTD
DATE DATEADD DATEDIFF DATENAME DATEPARSE DATEPART DATETRUNC DAY DEGREES DIV ELSE ELSEIF END ENDSWITH EXP FIND
FIXED FLOAT FLOOR IF IFNULL IIF INCLUDE EXCLUDE INDEX INT ISDATE ISNULL LEFT LEN LN LOG LOWER LTRIM MAX MEDIAN MID
MIN MONTH NOT NOW OR PI POWER QUARTER RADIANS RANK RANK_DENSE REPLACE RIGHT ROUND RTRIM RUNNING_AVG RUNNING_SUM
SIGN SIN SIZE SPLIT SQRT STARTSWITH STDEV STR SUM TAN THEN TODAY TOTAL TRIM UPPER VAR WEEK WHEN WINDOW_AVG
WINDOW_MAX WINDOW_MIN WINDOW_SUM YEAR ZN LOOKUP PREVIOUS_VALUE""".split())

_nullable = lambda t: {"anyOf": [t, {"type": "null"}]}  # noqa: E731
_value = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "calculations", "formats", "charts", "dashboard"],
    "properties": {
        "name": {"type": "string", "description": "Workbook name."},
        "calculations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "formula", "datatype"],
            "properties": {
                "name": {"type": "string"},
                "formula": {"type": "string", "description": "Tableau calculation syntax, fields as [Field Name]."},
                "datatype": {"enum": ["real", "integer", "string", "boolean", "date"]}}}},
        "formats": {"type": "array", "description": "Number formats for measures and calculations.", "items": {
            "type": "object", "additionalProperties": False, "required": ["field", "format"],
            "properties": {"field": {"type": "string"}, "format": {"enum": sorted(FORMATS)}}}},
        "charts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "chart", "fields", "filters", "sort", "reference_lines"],
            "properties": {
                "title": {"type": "string", "description": "The question the chart answers; used as the sheet name."},
                "chart": {"enum": list(CHARTS)},
                "fields": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["field", "agg", "role"],
                    "properties": {
                        "field": {"type": "string", "description": "Exact field name from the profile, or a calculation name."},
                        "agg": {"enum": ["auto", "none", *MEASURE_AGGS, *DATE_AGGS]},
                        "continuous_date": _nullable({"type": "boolean"}),
                        "role": {"enum": list(ROLES)}}}},
                "filters": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["field", "agg", "include", "exclude", "min", "max"],
                    "properties": {
                        "field": {"type": "string"},
                        "agg": {"enum": ["auto", "none", *MEASURE_AGGS]},
                        "include": _nullable({"type": "array", "items": _value}),
                        "exclude": _nullable({"type": "array", "items": _value}),
                        "min": _nullable({"type": "number"}),
                        "max": _nullable({"type": "number"})}}},
                "sort": _nullable({
                    "type": "object", "additionalProperties": False,
                    "required": ["field", "by_field", "by_agg", "direction"],
                    "properties": {
                        "field": {"type": "string"}, "by_field": {"type": "string"},
                        "by_agg": {"enum": ["auto", *MEASURE_AGGS]}, "direction": {"enum": ["asc", "desc"]}}}),
                "reference_lines": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["field", "agg", "formula"],
                    "properties": {
                        "field": {"type": "string"}, "agg": {"enum": ["auto", *MEASURE_AGGS]},
                        "formula": {"enum": sorted(REFLINE_FORMULAS)}}}},
            }}},
        "dashboard": _nullable({
            "type": "object", "additionalProperties": False,
            "required": ["name", "title", "charts", "filters", "orientation"],
            "properties": {
                "name": {"type": "string"},
                "title": _nullable({"type": "string"}),
                "filters": {"type": "array", "items": {"type": "string"},
                            "description": "Dimensions to show as dropdown filters that apply to every chart."},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "Chart titles, in order."},
                "orientation": {"enum": ["vertical", "horizontal"]}}}),
    },
}


class Planner:
    def __init__(self, plan: dict, csv_path: Path, separator: str = ","):
        self.plan = plan
        self.csv_path = csv_path
        self.bins: dict[str, float] = {}       # field -> bin size, for histograms
        self.errors: list[str] = []
        self.fields = {f.name.lower(): f for f in load_fields(csv_path, separator, {})}
        self.calcs = {c["name"].lower(): c for c in plan.get("calculations") or []}

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def resolve(self, name: str, where: str):
        key = name.strip().lower()
        if key in self.fields:
            f = self.fields[key]
            return f.name, f.datatype, f.role, False
        if key in self.calcs:
            c = self.calcs[key]
            agg = bool(AGG_IN_FORMULA.search(c["formula"]))
            role = "measure" if agg or c.get("datatype", "real") in ("real", "integer") else "dimension"
            return c["name"], c.get("datatype", "real"), role, agg
        known = [f.name for f in self.fields.values()] + [c["name"] for c in self.calcs.values()]
        close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
        self.err(where, f"unknown field {name!r}" + (f"; did you mean {close}?" if close else f"; known: {known}"))
        return None

    def pill(self, name: str, agg: str, continuous_date: bool | None, where: str, default_date: str):
        """Return (pill string, continuous?) or None."""
        r = self.resolve(name, where)
        if r is None:
            return None
        field, dtype, role, agg_calc = r
        if agg_calc:
            if agg not in ("auto", "none"):
                self.err(where, f"{field!r} is an aggregate calculation; use agg 'auto'")
            return field, True
        if dtype in ("date", "datetime"):
            if agg in ("auto", "none"):
                agg = default_date
            if agg not in DATE_AGGS:
                if agg in ("count", "countd"):
                    return f"{MEASURE_AGGS[agg]}({field})", True
                self.err(where, f"date field {field!r} needs a date part ({', '.join(DATE_AGGS)}) or count/countd")
                return None
            cont = continuous_date if continuous_date is not None else (default_date == "month")
            return (f"{DATE_AGGS[agg]}_TRUNC({field})" if cont else f"{DATE_AGGS[agg]}({field})"), cont
        if agg in DATE_AGGS:
            self.err(where, f"{agg!r} only applies to date fields; {field!r} is {dtype}")
            return None
        if role == "measure" or agg in MEASURE_AGGS:
            agg = "sum" if agg in ("auto", "none") else agg
            if dtype not in ("integer", "real") and agg not in ("count", "countd", "min", "max"):
                self.err(where, f"{agg!r} needs a numeric field; {field!r} is {dtype} (use count/countd)")
                return None
            return f"{MEASURE_AGGS[agg]}({field})", True
        return field, False

    def bin_size(self, field: str) -> float:
        """A round bin width giving roughly 20 bins over the field's range."""
        with self.csv_path.open(newline="", encoding="utf-8-sig") as fh:
            vals = [float(r[field]) for r in csv.DictReader(fh) if r.get(field, "").strip()]
        span = (max(vals) - min(vals)) if vals else 1.0
        raw = span / 20 or 1.0
        mag = 10 ** math.floor(math.log10(raw))
        return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)

    def calc_checks(self) -> None:
        names = {f.name.lower() for f in self.fields.values()} | set(self.calcs)
        for c in self.calcs.values():
            where = f"calculation {c['name']!r}"
            if c["name"].lower() in self.fields:
                self.err(where, "name collides with an existing field")
            f = c["formula"]
            if f.count("(") != f.count(")") or f.count("[") != f.count("]"):
                self.err(where, "unbalanced parentheses or brackets")
            for ref in re.findall(r"\[([^\]]+)\]", f):
                if ref.lower() not in names:
                    close = difflib.get_close_matches(ref, [x.name for x in self.fields.values()], n=2)
                    self.err(where, f"references unknown field [{ref}]" + (f"; did you mean {close}?" if close else ""))
            stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"|'[^']*'", " ", f)
            for fn in re.findall(r"\b([A-Za-z_]+)\s*\(", stripped):
                if fn.upper() not in FUNCTIONS:
                    self.err(where, f"unknown function {fn}()")

    def chart(self, c: dict) -> dict | None:
        title, kind = c["title"], c["chart"]
        where = f"chart {title!r}"
        if kind not in CHARTS:
            self.err(where, f"chart {kind!r} not supported; use one of {list(CHARTS)}")
            return None
        default_date = "month" if kind in ("line", "area", "combo", "dualline") else "year"
        placed: dict[str, list[tuple[str, bool]]] = {r: [] for r in ROLES}
        for f in c["fields"]:
            p = self.pill(f["field"], f.get("agg", "auto"), f.get("continuous_date"), where, default_date)
            if p:
                placed[f["role"]].append(p)
        x, y = placed["x"], placed["y"]
        cont = lambda ps: [p for p, k in ps if k]  # noqa: E731
        disc = lambda ps: [p for p, k in ps if not k]  # noqa: E731
        for axis, ps in (("x", x), ("y", y)):
            if cont(ps) and disc(ps):
                self.err(where, f"role {axis!r} mixes dimensions {disc(ps)} and measures {cont(ps)}; "
                                "put dimensions on one axis and measures on the other")
        need = {
            "bar": lambda: (cont(x) and not cont(y) and y) or (cont(y) and not cont(x) and x),
            "stackedbar": lambda: ((cont(x) and disc(y)) or (cont(y) and disc(x))) and disc(placed["color"]),
            "line": lambda: (cont(y) and x) or (cont(x) and y),
            "area": lambda: (cont(y) and x) or (cont(x) and y),
            "scatter": lambda: cont(x) and cont(y),
            "text": lambda: not cont(x) and not cont(y) and placed["label"],
            "highlight": lambda: disc(x) and disc(y) and cont(placed["color"]),
            "pie": lambda: not x and not y and cont(placed["size"]) and disc(placed["color"]),
            "treemap": lambda: not x and not y and cont(placed["size"]) and (placed["label"] or placed["color"]),
            "combo": lambda: x and not cont(x) or (x and len(cont(x)) == 1 and "_TRUNC(" in cont(x)[0]),
            "dualline": lambda: x and not cont(x) or (x and len(cont(x)) == 1 and "_TRUNC(" in cont(x)[0]),
            "histogram": lambda: True,
        }
        hint = {
            "bar": "dimension(s) on one axis (x or y) and measure(s) on the other",
            "stackedbar": "dimension on one axis, measure on the other, and a dimension on color",
            "line": "a date (or dimension) on x and measure(s) on y",
            "area": "a date (or dimension) on x and measure(s) on y",
            "scatter": "a measure on x and a measure on y (put the entity on detail)",
            "text": "dimensions on x/y (or none) and measure(s) on label",
            "highlight": "a dimension on x, a dimension on y, and a measure on color",
            "pie": "no x/y; a dimension on color and a measure on size",
            "treemap": "no x/y; a measure on size and a dimension on label or color",
            "combo": "a date or dimension on x and exactly two measures on y (first drawn as bars, second as a line)",
            "dualline": "a date or dimension on x and exactly two measures on y (two lines on synchronized axes)",
            "histogram": "one numeric field on x (counts of records per bin go on y automatically)",
        }
        if kind in ("combo", "dualline") and len(cont(y)) != 2:
            self.err(where, f"a {kind} chart needs {hint[kind]}")
        elif not need[kind]():
            self.err(where, f"a {kind} chart needs {hint[kind]}")
        ws = {"name": title, "mark": CHARTS[kind], "cols": [p for p, _ in x], "rows": [p for p, _ in y]}
        if kind in ("combo", "dualline") and len(cont(y)) == 2:
            ws["marks"] = ["Bar", "Line"] if kind == "combo" else ["Line", "Line"]
            ws["dual_axis"] = True
        if kind == "histogram":
            src = [f for f in c["fields"] if f["role"] == "x"]
            r = self.resolve(src[0]["field"], where) if len(src) == 1 and not y else None
            if r is None or r[1] not in ("integer", "real") or r[3]:
                if r is not None or len(src) != 1 or y:
                    self.err(where, f"a histogram chart needs {hint['histogram']}")
            else:
                self.bins[r[0]] = self.bin_size(r[0])
                ws["cols"], ws["rows"] = [f"{r[0]} (bin)"], [f"SUM({RECORDS})"]
        for role in ("color", "size", "label", "detail", "tooltip"):
            if placed[role]:
                key = "angle" if (kind == "pie" and role == "size") else role
                ws[key] = [p for p, _ in placed[role]]
        filters = []
        for flt in c.get("filters") or []:
            p = self.pill(flt["field"], flt.get("agg", "auto"), None, f"{where} filter", "year")
            if not p:
                continue
            pill, is_cont = p
            if flt.get("include") or flt.get("exclude"):
                if is_cont:
                    self.err(where, f"filter on {pill}: include/exclude lists need a dimension")
                    continue
                key = "include" if flt.get("include") else "exclude"
                filters.append({"field": pill, key: flt[key]})
            elif flt.get("min") is not None or flt.get("max") is not None:
                if not is_cont:
                    self.err(where, f"filter on {pill}: min/max needs a measure")
                    continue
                filters.append({"field": pill, **{k: flt[k] for k in ("min", "max") if flt.get(k) is not None}})
            else:
                self.err(where, f"filter on {flt['field']!r} needs include/exclude values or min/max")
        if filters:
            ws["filters"] = filters
        used_pills = {p for ps in placed.values() for p, _ in ps}
        lines = []
        for rl in c.get("reference_lines") or []:
            p = self.pill(rl["field"], rl.get("agg", "auto"), None, f"{where} reference line", default_date)
            if p and (not p[1] or p[0] not in used_pills):
                self.err(where, f"reference line on {p[0]}: needs a measure that is on the chart's x or y")
            elif p:
                lines.append({"measure": p[0], "formula": rl.get("formula", "average")})
        if lines:
            ws["reference_lines"] = lines
        s = c.get("sort")
        if s:
            on = self.pill(s["field"], "auto", None, f"{where} sort", default_date)
            by = self.pill(s["by_field"], s.get("by_agg", "auto"), None, f"{where} sort", default_date)
            used = {p for ps in placed.values() for p, _ in ps}
            if on and by:
                if on[1] or not by[1]:
                    self.err(where, "sort: 'field' must be a dimension and 'by_field' a measure")
                elif on[0] not in used or by[0] not in used:
                    self.err(where, f"sort: both {on[0]} and {by[0]} must be fields of the chart "
                                    "(add the measure to tooltip if it isn't shown)")
                else:
                    ws["sort"] = {"field": on[0], "by": by[0], "direction": s.get("direction", "desc")}
        return ws

    def to_vizspec(self, csv_rel: str) -> dict:
        self.calc_checks()
        sheets = [w for w in (self.chart(c) for c in self.plan.get("charts") or []) if w]
        if not self.plan.get("charts"):
            self.err("plan", "at least one chart is required")
        titles = [c["title"] for c in self.plan.get("charts") or []]
        if len(set(titles)) != len(titles):
            self.err("plan", "chart titles must be unique")
        spec = {"name": self.plan.get("name") or "Workbook", "datasource": {"csv": csv_rel},
                "calculations": [{"name": c["name"], "formula": c["formula"], "datatype": c.get("datatype", "real")}
                                 for c in self.plan.get("calculations") or []],
                "worksheets": sheets}
        if self.bins:
            spec["bins"] = [{"field": f, "size": size} for f, size in self.bins.items()]
            if RECORDS.lower() not in self.calcs and RECORDS.lower() not in self.fields:
                spec["calculations"].append({"name": RECORDS, "formula": "1", "datatype": "integer"})
        formats = {}
        for fmt in self.plan.get("formats") or []:
            r = self.resolve(fmt["field"], "formats")
            if r and r[2] != "measure":
                self.err("formats", f"{r[0]!r} is a dimension; number formats apply to measures and calculations")
            elif r:
                formats[r[0]] = fmt["format"]
        if formats:
            spec["formats"] = formats
        db = self.plan.get("dashboard")
        if db:
            missing = [t for t in db["charts"] if t not in titles]
            if missing:
                self.err("dashboard", f"unknown chart titles {missing}; use titles from 'charts'")
            dash = {"name": db["name"], "worksheets": db["charts"], "orientation": db.get("orientation", "vertical")}
            if db.get("title"):
                dash["title"] = db["title"]
            dims = []
            for f in db.get("filters") or []:
                r = self.resolve(f, "dashboard filters")
                if r and (r[2] != "dimension" or r[3]):
                    self.err("dashboard", f"filter {r[0]!r} must be a dimension (dropdown of its values)")
                elif r:
                    dims.append(r[0])
            if dims:
                dash["filters"] = dims
            spec["dashboards"] = [dash]
        return spec


def plan_to_vizspec(plan: dict, csv_path: Path, csv_rel: str | None = None) -> tuple[dict, list[str]]:
    p = Planner(plan, csv_path)
    spec = p.to_vizspec(csv_rel or csv_path.name)
    return spec, p.errors


if __name__ == "__main__":
    plan_file, csv = Path(sys.argv[1]), Path(sys.argv[2])
    spec, errors = plan_to_vizspec(json.loads(plan_file.read_text()), csv)
    print(json.dumps({"errors": errors, "vizspec": spec}, indent=1))
