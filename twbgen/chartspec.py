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
from spec_to_twb import AGG_IN_FORMULA, FORMATS, PARAM_TYPES, REFLINE_FORMULAS, load_model  # noqa: E402

CHARTS = {  # chart -> mark
    "bar": "Bar", "stackedbar": "Bar", "line": "Line", "area": "Area", "scatter": "Circle",
    "text": "Text", "highlight": "Square", "pie": "Pie", "treemap": "Square",
    "combo": "Automatic", "dualline": "Automatic", "histogram": "Bar",
    "filledmap": "Automatic", "symbolmap": "Circle", "pointmap": "Circle",
}
MAPS = {"filledmap": "filled", "symbolmap": "symbol", "pointmap": "points"}
AREA_GEO = {"country", "state", "county", "city", "zip"}
RECORDS = "Number of Records"
MEASURE_AGGS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "median": "MEDIAN",
                "count": "COUNT", "countd": "COUNTD"}
DATE_AGGS = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK", "day": "DAY"}
ROLES = ("x", "y", "color", "size", "label", "detail", "tooltip", "geo")
ACTIONS = ("filter", "highlight", "parameter")
FUNCTIONS = set("""ABS ACOS AND ASCII ASIN ATAN ATAN2 ATTR AVG CASE CEILING CHAR CONTAINS COS COT COUNT COUNTD
DATE DATEADD DATEDIFF DATENAME DATEPARSE DATEPART DATETRUNC DAY DEGREES DIV ELSE ELSEIF END ENDSWITH EXP FIND
FIXED FLOAT FLOOR IF IFNULL IIF INCLUDE EXCLUDE INDEX INT ISDATE ISNULL LEFT LEN LN LOG LOWER LTRIM MAX MEDIAN MID
MIN MONTH NOT NOW OR PI POWER QUARTER RADIANS RANK RANK_DENSE REPLACE RIGHT ROUND RTRIM RUNNING_AVG RUNNING_SUM
SIGN SIN SIZE SPLIT SQRT STARTSWITH STDEV STR SUM TAN THEN TODAY TOTAL TRIM UPPER VAR WEEK WHEN WINDOW_AVG
WINDOW_MAX WINDOW_MIN WINDOW_SUM YEAR ZN LOOKUP PREVIOUS_VALUE""".split())

_nullable = lambda t: {"anyOf": [t, {"type": "null"}]}  # noqa: E731
_value = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}
_measure_agg = {"enum": ["auto", *MEASURE_AGGS]}
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "data_model", "parameters", "calculations", "formats", "charts", "dashboard"],
    "properties": {
        "name": {"type": "string", "description": "Workbook name."},
        "data_model": _nullable({
            "type": "object", "additionalProperties": False, "required": ["links"],
            "description": "Only with two or more CSVs: how the tables connect.",
            "properties": {"links": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "left", "right", "left_field", "right_field", "join_type"],
                "properties": {
                    "kind": {"enum": ["relationship", "join"]},
                    "left": {"type": "string", "description": "Table (CSV file name) already in the model."},
                    "right": {"type": "string", "description": "Table being linked in."},
                    "left_field": {"type": "string"}, "right_field": {"type": "string"},
                    "join_type": _nullable({"enum": ["inner", "left"]})}}}}}),
        "parameters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "datatype", "value", "allowed", "min", "max", "step"],
            "properties": {
                "name": {"type": "string"}, "datatype": {"enum": sorted(PARAM_TYPES)}, "value": _value,
                "allowed": _nullable({"type": "array", "items": _value}),
                "min": _nullable({"type": "number"}), "max": _nullable({"type": "number"}),
                "step": _nullable({"type": "number"})}}},
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
                    "required": ["field", "agg", "include", "exclude", "min", "max", "top", "by_field", "by_agg"],
                    "properties": {
                        "field": {"type": "string"},
                        "agg": {"enum": ["auto", "none", *MEASURE_AGGS]},
                        "include": _nullable({"type": "array", "items": _value}),
                        "exclude": _nullable({"type": "array", "items": _value}),
                        "min": _nullable({"type": "number"}),
                        "max": _nullable({"type": "number"}),
                        "top": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}],
                                "description": "Keep the top N members, a number or an integer parameter's name."},
                        "by_field": _nullable({"type": "string"}),
                        "by_agg": _nullable(_measure_agg)}}},
                "sort": _nullable({
                    "type": "object", "additionalProperties": False,
                    "required": ["field", "by_field", "by_agg", "direction"],
                    "properties": {
                        "field": {"type": "string"}, "by_field": {"type": "string"},
                        "by_agg": _measure_agg, "direction": {"enum": ["asc", "desc"]}}}),
                "reference_lines": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["field", "agg", "formula"],
                    "properties": {
                        "field": {"type": "string"}, "agg": _measure_agg,
                        "formula": {"enum": sorted(REFLINE_FORMULAS)}}}},
            }}},
        "dashboard": _nullable({
            "type": "object", "additionalProperties": False,
            "required": ["name", "title", "charts", "filters", "parameters", "actions", "orientation"],
            "properties": {
                "name": {"type": "string"},
                "title": _nullable({"type": "string"}),
                "filters": {"type": "array", "items": {"type": "string"},
                            "description": "Dimensions to show as dropdown filters that apply to every chart."},
                "parameters": {"type": "array", "items": {"type": "string"},
                               "description": "Parameters to show as controls."},
                "actions": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["type", "source", "targets", "field", "parameter", "on"],
                    "properties": {
                        "type": {"enum": list(ACTIONS)},
                        "source": {"type": "string", "description": "Chart title the reader clicks or hovers."},
                        "targets": _nullable({"type": "array", "items": {"type": "string"}}),
                        "field": _nullable({"type": "string"}),
                        "parameter": _nullable({"type": "string"}),
                        "on": _nullable({"enum": ["select", "hover"]})}}},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "Chart titles, in order."},
                "orientation": {"enum": ["vertical", "horizontal"]}}}),
    },
}


class Planner:
    def __init__(self, plan: dict, csv_paths: list[Path], separator: str = ","):
        self.plan = plan
        self.tables = load_model(csv_paths, separator)
        self.bins: dict[str, float] = {}       # field -> bin size, for histograms
        self.errors: list[str] = []
        self.fields = {f.name.lower(): f for t in self.tables for f in t.fields}
        self.calcs = {c["name"].lower(): c for c in plan.get("calculations") or []}
        self.params = {p["name"].lower(): p for p in plan.get("parameters") or []}

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def resolve(self, name: str, where: str):
        """(name, datatype, role, is-aggregate-calc, geo) for a field or calculation, else None."""
        key = name.strip().lower()
        if key in self.fields:
            f = self.fields[key]
            return f.name, f.datatype, f.role, False, f.geo
        if key in self.calcs:
            c = self.calcs[key]
            agg = bool(AGG_IN_FORMULA.search(c["formula"]))
            role = "measure" if agg or c.get("datatype", "real") in ("real", "integer") else "dimension"
            return c["name"], c.get("datatype", "real"), role, agg, None
        known = [f.name for f in self.fields.values()] + [c["name"] for c in self.calcs.values()]
        close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
        hint = "; parameters are used through calculations" if key in self.params else ""
        self.err(where, f"unknown field {name!r}" + (f"; did you mean {close}?" if close else f"; known: {known}") + hint)
        return None

    def pill(self, name: str, agg: str, continuous_date: bool | None, where: str, default_date: str):
        """Return (pill string, continuous?) or None."""
        r = self.resolve(name, where)
        if r is None:
            return None
        field, dtype, role, agg_calc, geo = r
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
            if agg in ("auto", "none"):
                agg = "avg" if geo in ("latitude", "longitude") else "sum"
            if dtype not in ("integer", "real") and agg not in ("count", "countd", "min", "max"):
                self.err(where, f"{agg!r} needs a numeric field; {field!r} is {dtype} (use count/countd)")
                return None
            return f"{MEASURE_AGGS[agg]}({field})", True
        return field, False

    def bin_size(self, field: str) -> float:
        """A round bin width giving roughly 20 bins over the field's range."""
        f = self.fields[field.lower()]
        table = next(t for t in self.tables if t.name == f.table)
        with table.path.open(newline="", encoding="utf-8-sig") as fh:
            vals = [float(r[f.remote]) for r in csv.DictReader(fh) if r.get(f.remote, "").strip()]
        span = (max(vals) - min(vals)) if vals else 1.0
        raw = span / 20 or 1.0
        mag = 10 ** math.floor(math.log10(raw))
        return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)

    def calc_checks(self) -> None:
        names = {f.name.lower() for f in self.fields.values()} | set(self.calcs) | set(self.params)
        for c in self.calcs.values():
            where = f"calculation {c['name']!r}"
            if c["name"].lower() in self.fields:
                self.err(where, "name collides with an existing field")
            f = c["formula"]
            if f.count("(") != f.count(")") or f.count("[") != f.count("]"):
                self.err(where, "unbalanced parentheses or brackets")
            for p in re.findall(r"\[Parameters\]\.\[([^\]]+)\]", f):
                if p.lower() not in self.params:
                    self.err(where, f"unknown parameter [{p}]; declare it in 'parameters'")
            body = re.sub(r"\[Parameters\]\.\[[^\]]+\]", " ", f)
            for ref in re.findall(r"\[([^\]]+)\]", body):
                if ref.lower() not in names:
                    close = difflib.get_close_matches(ref, [x.name for x in self.fields.values()], n=2)
                    self.err(where, f"references unknown field [{ref}]" + (f"; did you mean {close}?" if close else ""))
            stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"|'[^']*'", " ", body)
            for fn in re.findall(r"\b([A-Za-z_]+)\s*\(", stripped):
                if fn.upper() not in FUNCTIONS:
                    self.err(where, f"unknown function {fn}()")

    def param_checks(self) -> list[dict]:
        out = []
        for p in self.plan.get("parameters") or []:
            where = f"parameter {p['name']!r}"
            if p["name"].lower() in self.fields or p["name"].lower() in self.calcs:
                self.err(where, "name collides with a field or calculation")
            spec = {"name": p["name"], "datatype": p["datatype"], "value": p["value"]}
            if p.get("allowed"):
                if p.get("min") is not None or p.get("max") is not None:
                    self.err(where, "give 'allowed' values or a min/max range, not both")
                if p["value"] not in p["allowed"]:
                    self.err(where, f"value {p['value']!r} must be one of 'allowed'")
                spec["list"] = p["allowed"]
            elif p.get("min") is not None or p.get("max") is not None:
                if p["datatype"] not in ("integer", "real", "date"):
                    self.err(where, "a min/max range needs an integer, real or date parameter")
                spec["range"] = {k: p[k] for k in ("min", "max", "step") if p.get(k) is not None}
            out.append(spec)
        return out

    def chart(self, c: dict) -> dict | None:
        title, kind = c["title"], c["chart"]
        where = f"chart {title!r}"
        if kind not in CHARTS:
            self.err(where, f"chart {kind!r} not supported; use one of {list(CHARTS)}")
            return None
        default_date = "month" if kind in ("line", "area", "combo", "dualline") else "year"
        placed: dict[str, list[tuple[str, bool]]] = {r: [] for r in ROLES}
        geo_kinds = []
        for f in c["fields"]:
            p = self.pill(f["field"], f.get("agg", "auto"), f.get("continuous_date"), where, default_date)
            if p:
                placed[f["role"]].append(p)
                if f["role"] == "geo":
                    r = self.resolve(f["field"], where)
                    geo_kinds.append(r[4] if r else None)
        x, y, geo = placed["x"], placed["y"], placed["geo"]
        cont = lambda ps: [p for p, k in ps if k]  # noqa: E731
        disc = lambda ps: [p for p, k in ps if not k]  # noqa: E731
        for axis, ps in (("x", x), ("y", y)):
            if cont(ps) and disc(ps):
                self.err(where, f"role {axis!r} mixes dimensions {disc(ps)} and measures {cont(ps)}; "
                                "put dimensions on one axis and measures on the other")
        if geo and kind not in ("filledmap", "symbolmap"):
            self.err(where, "role 'geo' is only for filledmap and symbolmap charts")
        if kind in ("filledmap", "symbolmap") and any(g not in AREA_GEO for g in geo_kinds):
            self.err(where, "geo fields must have a geographic role (country, state, county, city or zip in the profile)")
        lat_lon = lambda ps, kind_: [p for p, _ in ps if (self.resolve(re.sub(r"^[A-Z]+\((.*)\)$", r"\1", p), where)  # noqa: E731
                                                          or [None] * 5)[4] == kind_]
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
            "filledmap": lambda: geo and not x and not y and cont(placed["color"]),
            "symbolmap": lambda: geo and not x and not y and cont(placed["size"]),
            "pointmap": lambda: len(x) == 1 and len(y) == 1 and lat_lon(x, "longitude") and lat_lon(y, "latitude"),
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
            "filledmap": "no x/y; a geographic field on geo and a measure on color",
            "symbolmap": "no x/y; geographic field(s) on geo (e.g. State then City) and a measure on size",
            "pointmap": "a longitude field on x and a latitude field on y (agg auto = average), the entity on detail",
        }
        if kind in ("combo", "dualline") and len(cont(y)) != 2:
            self.err(where, f"a {kind} chart needs {hint[kind]}")
        elif not need[kind]():
            self.err(where, f"a {kind} chart needs {hint[kind]}")
        ws = {"name": title, "mark": CHARTS[kind], "cols": [p for p, _ in x], "rows": [p for p, _ in y]}
        if kind in MAPS:
            ws["map"] = MAPS[kind]
            if geo:
                ws["geo"] = [p for p, _ in geo]
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
            if flt.get("top") is not None:
                if is_cont:
                    self.err(where, f"top-N filter on {pill}: needs a dimension")
                    continue
                if not flt.get("by_field"):
                    self.err(where, f"top-N filter on {pill}: give by_field (the measure to rank by)")
                    continue
                by = self.pill(flt["by_field"], flt.get("by_agg") or "auto", None, f"{where} filter", "year")
                top = flt["top"]
                if isinstance(top, str):
                    prm = self.params.get(top.lower())
                    if prm is None or prm["datatype"] != "integer":
                        self.err(where, f"top-N count {top!r} must be a number or an integer parameter")
                        continue
                if by and not by[1]:
                    self.err(where, f"top-N filter: by_field {flt['by_field']!r} must be a measure")
                elif by:
                    filters.append({"field": pill, "top": top, "by": by[0]})
            elif flt.get("include") or flt.get("exclude"):
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
                self.err(where, f"filter on {flt['field']!r} needs include/exclude values, min/max, or top + by_field")
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
            if on and by:
                if on[1] or not by[1]:
                    self.err(where, "sort: 'field' must be a dimension and 'by_field' a measure")
                elif on[0] not in used_pills or by[0] not in used_pills:
                    self.err(where, f"sort: both {on[0]} and {by[0]} must be fields of the chart "
                                    "(add the measure to tooltip if it isn't shown)")
                else:
                    ws["sort"] = {"field": on[0], "by": by[0], "direction": s.get("direction", "desc")}
        return ws

    def data_model(self) -> dict:
        names = [t.name for t in self.tables]
        model = self.plan.get("data_model") or {}
        links = model.get("links") or []
        if len(self.tables) == 1:
            if links:
                self.err("data_model", "links need two or more CSVs")
            return {}
        if not links:
            self.err("data_model", f"tables {names} need links (relationship recommended)")
            return {}
        kinds = {link["kind"] for link in links}
        if len(kinds) > 1:
            self.err("data_model", "use only relationships or only joins")
        out = []
        for i, link in enumerate(links):
            where = f"data_model link {i + 1}"
            ends = []
            for side in ("left", "right"):
                t = next((t for t in self.tables if t.name.lower() == link[side].lower()
                          or t.path.stem.lower() == link[side].lower()), None)
                if t is None:
                    self.err(where, f"unknown table {link[side]!r}; tables: {names}")
                    break
                fld = next((f for f in t.fields if link[f"{side}_field"].lower() in (f.name.lower(), f.remote.lower())), None)
                if fld is None:
                    self.err(where, f"{link[side + '_field']!r} isn't a column of {t.name}: "
                                    f"{[f.name for f in t.fields]}")
                    break
                ends.append((t.name, fld.remote))
            if len(ends) == 2:
                item = {"left": ends[0][0], "right": ends[1][0], "on": [[ends[0][1], ends[1][1]]]}
                if link["kind"] == "join":
                    item["type"] = link.get("join_type") or "inner"
                out.append(item)
        return {("joins" if "join" in kinds else "relationships"): out}

    def to_vizspec(self, csv_rel: list[str]) -> dict:
        self.calc_checks()
        params = self.param_checks()
        sheets = [w for w in (self.chart(c) for c in self.plan.get("charts") or []) if w]
        if not self.plan.get("charts"):
            self.err("plan", "at least one chart is required")
        titles = [c["title"] for c in self.plan.get("charts") or []]
        if len(set(titles)) != len(titles):
            self.err("plan", "chart titles must be unique")
        datasource = {"csv": csv_rel[0]} if len(csv_rel) == 1 else {"tables": csv_rel, **self.data_model()}
        if len(csv_rel) == 1:
            self.data_model()
        spec = {"name": self.plan.get("name") or "Workbook", "datasource": datasource,
                "calculations": [{"name": c["name"], "formula": c["formula"], "datatype": c.get("datatype", "real")}
                                 for c in self.plan.get("calculations") or []],
                "worksheets": sheets}
        if params:
            spec["parameters"] = params
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
            spec["dashboards"] = [self.dashboard(db, titles, {w["name"]: w for w in sheets})]
        return spec

    def dashboard(self, db: dict, titles: list[str], sheets: dict[str, dict]) -> dict:
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
        controls = []
        for p in db.get("parameters") or []:
            if p.lower() not in self.params:
                self.err("dashboard", f"parameter control {p!r} isn't declared in 'parameters'")
            else:
                controls.append(self.params[p.lower()]["name"])
        if controls:
            dash["parameters"] = controls
        actions = []
        for i, a in enumerate(db.get("actions") or []):
            where = f"dashboard action {i + 1}"
            if a["source"] not in db["charts"]:
                self.err(where, f"source {a['source']!r} must be a chart on the dashboard")
                continue
            bad = [t for t in a.get("targets") or [] if t not in db["charts"] or t == a["source"]]
            if bad:
                self.err(where, f"targets {bad} must be other charts on the dashboard")
            act = {"type": a["type"], "source": a["source"]}
            src = sheets.get(a["source"], {})
            shown = [p for key in ("rows", "cols", "color", "detail", "label", "geo")
                     for p in (src.get(key) or [])]
            dims = [p for p in shown if self.fields.get(p.lower()) is not None
                    and self.fields[p.lower()].role == "dimension"
                    and self.fields[p.lower()].datatype not in ("date", "datetime")]
            if a.get("field"):
                r = self.resolve(a["field"], where)
                if r and r[0] not in dims:
                    self.err(where, f"field {r[0]!r} must be a non-date dimension shown on {a['source']!r} "
                                    f"(it shows {dims or 'none'})")
                elif r:
                    act["field"] = r[0]
            elif not dims:
                self.err(where, f"{a['source']!r} shows no non-date dimension to act on; add one or give 'field'")
            if a.get("targets"):
                act["targets"] = a["targets"]
            if a.get("on") == "hover":
                act["on"] = "hover"
            if a["type"] == "parameter":
                prm = self.params.get((a.get("parameter") or "").lower())
                if prm is None:
                    self.err(where, f"parameter action needs a declared parameter, got {a.get('parameter')!r}")
                else:
                    fname = act.get("field") or (dims[0] if dims else None)
                    fdt = self.fields[fname.lower()].datatype if fname and fname.lower() in self.fields else None
                    if fdt and prm["datatype"] != fdt:
                        self.err(where, f"parameter {prm['name']!r} is {prm['datatype']} but {fname!r} is {fdt}")
                    act["parameter"] = prm["name"]
            actions.append(act)
        if actions:
            dash["actions"] = actions
        return dash


def plan_to_vizspec(plan: dict, csv_paths: Path | list[Path], csv_rel: list[str] | None = None) -> tuple[dict, list[str]]:
    paths = [csv_paths] if isinstance(csv_paths, Path) else list(csv_paths)
    p = Planner(plan, paths)
    spec = p.to_vizspec(csv_rel or [c.name for c in paths])
    return spec, p.errors


if __name__ == "__main__":
    plan_file, *csvs = sys.argv[1:]
    spec, errors = plan_to_vizspec(json.loads(Path(plan_file).read_text()), [Path(c) for c in csvs])
    print(json.dumps({"errors": errors, "vizspec": spec}, indent=1))
