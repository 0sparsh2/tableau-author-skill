"""Describe any .twb in the same spec grammar spec_to_twb.py compiles.

This is the "read" half of an improve loop: an LLM gets a compact, faithful
summary (sheets, shelves, marks, encodings, filters, calcs, dashboards)
instead of megabytes of XML, proposes an edited spec, and the compiler
writes it back. Desktop-saved files are normalized first (FCP wrappers
resolved), so 2020.x-2026.x workbooks read the same way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, also under `python -I`
from normalize import normalize, read_twb  # noqa: E402

PREFIX_TO_FUNC = {
    "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "med": "MEDIAN", "cnt": "COUNT",
    "ctd": "COUNTD", "attr": "ATTR", "stdev": "STDEV", "var": "VAR",
    "yr": "YEAR", "qr": "QUARTER", "mn": "MONTH", "wk": "WEEK", "dy": "DAY",
    "tyr": "YEAR_TRUNC", "tqr": "QUARTER_TRUNC", "tmn": "MONTH_TRUNC", "twk": "WEEK_TRUNC", "tdy": "DAY_TRUNC",
}
ENCODING_TO_KEY = {"color": "color", "size": "size", "text": "label", "shape": "shape", "lod": "detail",
                   "tooltip": "tooltip", "wedge-size": "angle", "path": "path", "geometry": "geometry"}
INSTANCE = re.compile(r"^\[(?P<prefix>[a-z]*):(?P<field>.+):(?P<suffix>[a-z]+)(?::\d+)?\]$")
PSEUDO_FIELDS = {"[:Measure Names]": "Measure Names", "[Multiple Values]": "Measure Values"}


def split_top(expr: str) -> tuple[list[str], str | None]:
    """Split a shelf expression on its top-level operator (+, /, *), respecting brackets/parens."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")") and _balanced(expr[1:-1]):
        expr = expr[1:-1].strip()
    parts, ops, depth_b, depth_p, cur = [], set(), 0, 0, []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "[":
            depth_b += 1
        elif ch == "]":
            if i + 1 < len(expr) and expr[i + 1] == "]" and depth_b:  # escaped ]]
                cur.append("]]"); i += 2; continue
            depth_b -= 1
        elif ch == "(" and not depth_b:
            depth_p += 1
        elif ch == ")" and not depth_b:
            depth_p -= 1
        if not depth_b and not depth_p and expr[i:i + 3] in (" + ", " / ", " * "):
            ops.add(expr[i + 1]); parts.append("".join(cur).strip()); cur = []; i += 3; continue
        cur.append(ch); i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p], (ops.pop() if len(ops) == 1 else ("mixed" if ops else None))


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return False
    return depth == 0


class Describer:
    def __init__(self, root: ET.Element):
        self.root = root
        self.captions: dict[str, str] = {}
        for col in root.iter("column"):
            if col.get("caption") and col.get("name"):
                self.captions[col.get("name")] = col.get("caption")

    def pill(self, qualified: str) -> str:
        local = qualified.split("].[", 1)[1] if "].[" in qualified else qualified
        local = local if local.startswith("[") else "[" + local
        if local in PSEUDO_FIELDS:
            return PSEUDO_FIELDS[local]
        m = INSTANCE.match(local)
        if not m:
            return local.strip("[]")
        field = m.group("field").replace("]]", "]")
        label = self.captions.get(f"[{field}]", field)
        func = PREFIX_TO_FUNC.get(m.group("prefix"))
        return f"{func}({label})" if func else label

    def shelf(self, text: str | None) -> dict | None:
        if not text or not text.strip():
            return None
        terms, op = split_top(text)
        flat: list[str] = []
        ops = {op} if op else set()

        def flatten(term: str) -> None:
            sub, sub_op = split_top(term)
            if len(sub) == 1:
                flat.append(sub[0])
                return
            ops.add(sub_op)
            for s in sub:
                flatten(s)

        for t in terms:
            flatten(t)
        op = ops.pop() if len(ops) == 1 else ("mixed" if ops else None)
        return {"pills": [self.pill(t) for t in flat], "operator": {"+": "concat", "/": "nest", "*": "cross"}.get(op, op)}

    def describe(self) -> dict:
        r = self.root
        out = {"version": r.get("version"), "datasources": [], "worksheets": [], "dashboards": []}
        for ds in r.find("datasources") if r.find("datasources") is not None else []:
            conn = ds.find("connection")
            fields, calcs = [], []
            for col in ds.findall("column"):
                if col.get("datatype") == "table":
                    continue
                calc = col.find("calculation")
                label = col.get("caption") or col.get("name", "").strip("[]")
                if calc is not None and calc.get("formula") is not None:
                    calcs.append({"name": label, "formula": calc.get("formula"), "datatype": col.get("datatype")})
                else:
                    fields.append({"name": label, "datatype": col.get("datatype"), "role": col.get("role")})
            out["datasources"].append({"name": ds.get("caption") or ds.get("name"),
                                       "connection": conn.get("class") if conn is not None else None,
                                       "fields_with_metadata": fields, "calculations": calcs})
        for ws in r.iter("worksheet"):
            table = ws.find("table")
            if table is None:
                continue
            view = table.find("view")
            panes = []
            for pane in table.iter("pane"):
                mark = pane.find("mark")
                enc: dict[str, list[str]] = {}
                for e in (pane.find("encodings") if pane.find("encodings") is not None else []):
                    enc.setdefault(ENCODING_TO_KEY.get(e.tag, e.tag), []).append(self.pill(e.get("column", "")))
                panes.append({"mark": mark.get("class") if mark is not None else None, **enc})
            filters = []
            for f in (view.findall("filter") if view is not None else []):
                item = {"field": self.pill(f.get("column", "")), "class": f.get("class")}
                members = [g.get("member") for g in f.iter("groupfilter") if g.get("function") == "member"]
                if members:
                    values = [json.loads(m) if m and m.startswith('"') else (m or "") for m in members]
                    # Measure Names filters list field references, not data values.
                    item["values"] = [self.pill(v) if isinstance(v, str) and v.startswith("[") and "].[" in v else v
                                      for v in values]
                for bound in ("min", "max"):
                    if f.find(bound) is not None:
                        item[bound] = f.findtext(bound)
                filters.append(item)
            out["worksheets"].append({"name": ws.get("name"), "rows": self.shelf(table.findtext("rows")),
                                      "cols": self.shelf(table.findtext("cols")), "panes": panes,
                                      "filters": filters})
        for db in r.iter("dashboard"):
            size = db.find("size")
            sheets = [z.get("name") for z in db.iter("zone") if z.get("name") and z.get("type-v2") is None]
            out["dashboards"].append({"name": db.get("name"), "size": dict(size.attrib) if size is not None else None,
                                      "worksheets": sheets,
                                      "zone_count": sum(1 for _ in db.iter("zone"))})
        return out


def describe_file(path: str) -> dict:
    root = read_twb(path)
    normalize(root, root.get("version") or "26.2")
    return Describer(root).describe()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb")
    a = p.parse_args(argv)
    print(json.dumps(describe_file(a.twb), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
