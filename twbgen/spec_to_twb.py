"""Compile a small, LLM-friendly viz spec (JSON) into a Tableau workbook (.twb/.twbx).

The spec is the intermediate representation an LLM emits; this compiler owns
every Tableau-specific detail (internal names, column-instance derivations,
shelf algebra, datasource XML, windows) so the model never writes raw TWB XML.
Output mirrors what Tableau Desktop itself writes: format version 18.1 plus an
explicit feature manifest. `<ManifestByVersion/>` is avoided because Desktop
2026.3 enables in-development format flags for it (MarkTypeAttribute,
SheetNumTstAddOnly) and rejects the workbook; see desktop_check.py.

Pill grammar (strings in rows/cols/encodings):
    "Region"                -> dimension, or SUM() for a numeric measure
    "SUM(Sales)"            -> SUM AVG MIN MAX MEDIAN COUNT COUNTD ATTR STDEV VAR
    "MONTH(Order Date)"     -> discrete date part: YEAR QUARTER MONTH WEEK DAY
    "MONTH_TRUNC(Order Date)" -> continuous date: YEAR_TRUNC ... DAY_TRUNC
    "Profit Ratio"          -> a calculation from spec["calculations"]
Shelf algebra: discrete pills are nested (`/`), continuous pills concatenated
(`+`). Mixing discrete and continuous pills on one shelf is rejected because
its serialization has not been verified against a Desktop round-trip.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import uuid
import zipfile
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

USER_NS = "http://www.tableausoftware.com/xml/user"
VERSION = "18.1"  # the format version Desktop 2020.x-2026.x writes
# Feature flags Desktop turns on for validation; each one we emit relies on it.
MANIFEST_FLAGS = (
    "AnimationOnByDefault", "Extensions", "MarkAnimation", "ObjectModelEncapsulateLegacy",
    "ObjectModelExtractV2", "ObjectModelTableType", "SchemaViewerObjectModel", "SetMembershipControl",
    "SheetIdentifierTracking", "VConnDownstreamExtractsWithWarnings", "WindowsPersistSimpleIdentifiers",
)
REMOTE_TYPE = {"integer": "20", "real": "5", "string": "129", "date": "133", "datetime": "135", "boolean": "11"}
DEFAULT_AGG = {"integer": "Sum", "real": "Sum", "string": "Count", "date": "Year", "datetime": "Year", "boolean": "Count"}

# function -> (derivation attribute, name prefix, discrete?)
MEASURE_FUNCS = {
    "SUM": ("Sum", "sum"), "AVG": ("Avg", "avg"), "MIN": ("Min", "min"), "MAX": ("Max", "max"),
    "MEDIAN": ("Median", "med"), "COUNT": ("Count", "cnt"), "COUNTD": ("CountD", "ctd"),
    "ATTR": ("Attribute", "attr"), "STDEV": ("Stdev", "stdev"), "VAR": ("Var", "var"),
}
DATE_PARTS = {"YEAR": ("Year", "yr"), "QUARTER": ("Quarter", "qr"), "MONTH": ("Month", "mn"),
              "WEEK": ("Week", "wk"), "DAY": ("Day", "dy")}
DATE_TRUNCS = {f"{k}_TRUNC": (f"{v[0]}-Trunc", "t" + v[1]) for k, v in DATE_PARTS.items()}
AGG_IN_FORMULA = re.compile(r"\b(SUM|AVG|MIN|MAX|MEDIAN|COUNT|COUNTD|ATTR|STDEV|VAR|WINDOW_\w+|RUNNING_\w+)\s*\(", re.I)
PILL = re.compile(r"^\s*([A-Z_]+)\s*\(\s*(.+?)\s*\)\s*$")
MARKS = {"Automatic", "Text", "Shape", "Square", "Circle", "Bar", "GanttBar", "Line", "Area", "Pie", "Polygon", "Heatmap"}
ENCODINGS = {"color": "color", "size": "size", "label": "text", "text": "text", "shape": "shape",
             "detail": "lod", "tooltip": "tooltip", "angle": "wedge-size"}
# Number formats as Desktop writes them in a column's default-format attribute.
FORMATS = {"currency": 'c"$"#,##0;("$"#,##0)', "currency2": 'c"$"#,##0.00;("$"#,##0.00)',
           "percent": "p0.0%", "number": "n#,##0", "decimal": "n#,##0.00"}
REFLINE_FORMULAS = {"average", "median", "min", "max"}
MEASURE_NAMES = "[:Measure Names]"
MAP_MODES = {"filled", "symbol", "points"}
# Geographic roles, keyed by normalized column name; values are Tableau semantic roles.
GEO_ALIASES = {"country": "country", "nation": "country", "state": "state", "province": "state",
               "state/province": "state", "city": "city", "county": "county", "zip": "zip", "zip code": "zip",
               "zipcode": "zip", "postal code": "zip", "postcode": "zip", "latitude": "latitude", "lat": "latitude",
               "longitude": "longitude", "lon": "longitude", "lng": "longitude", "long": "longitude"}
SEMANTIC_ROLES = {"country": "[Country].[ISO3166_2]", "state": "[State].[Name]", "city": "[City].[Name]",
                  "county": "[County].[Name]", "zip": "[ZipCode].[Name]",
                  "latitude": "[Geographical].[Latitude]", "longitude": "[Geographical].[Longitude]"}


class SpecError(ValueError):
    pass


def bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def stable_id(*parts: str, length: int = 28) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    n = int.from_bytes(digest, "big")
    out = []
    while len(out) < length:
        n, r = divmod(n, 36)
        out.append(alphabet[r])
    return "".join(out)


def stable_uuid(*parts: str) -> str:
    return "{" + str(uuid.UUID(hashlib.md5("\x1f".join(parts).encode()).hexdigest())).upper() + "}"


# ---------------------------------------------------------------- data model
@dataclass
class Field:
    name: str
    datatype: str
    role: str          # dimension | measure
    type: str          # nominal | ordinal | quantitative
    formula: str | None = None
    caption: str | None = None
    calc_class: str = "tableau"       # "bin" for histogram bins
    bin_size: float | None = None
    default_format: str | None = None
    aggregate: bool | None = None     # set for calcs that build on aggregate calcs
    table: str | None = None          # source CSV file name, for physical columns
    remote: str | None = None         # column name inside that CSV
    geo: str | None = None            # key of SEMANTIC_ROLES

    @property
    def semantic_role(self) -> str | None:
        return SEMANTIC_ROLES.get(self.geo) if self.geo else None

    @property
    def internal(self) -> str:
        return bracket(self.name)

    @property
    def is_aggregate_calc(self) -> bool:
        if self.aggregate is not None:
            return self.aggregate
        return bool(self.formula and self.calc_class == "tableau" and AGG_IN_FORMULA.search(self.formula))


def infer_datatype(values: list[str]) -> str:
    vals = [v.strip() for v in values if v.strip() != ""]
    if not vals:
        return "string"
    def all_match(fn):
        try:
            for v in vals:
                fn(v)
            return True
        except ValueError:
            return False
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    if all_match(int):
        return "integer"
    if all_match(float):
        return "real"
    if all_match(lambda v: datetime.strptime(v, "%Y-%m-%d")):
        return "date"
    if all_match(datetime.fromisoformat):
        return "datetime"
    return "string"


def load_fields(csv_path: Path, separator: str, overrides: dict) -> list[Field]:
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=separator))
    if not rows:
        raise SpecError(f"{csv_path} is empty")
    header, sample = rows[0], rows[1:501]
    fields = []
    for i, col in enumerate(header):
        dt = overrides.get(col, {}).get("datatype") or infer_datatype([r[i] for r in sample if i < len(r)])
        is_id = re.search(r"(^|[\s_])id$", col, re.I) is not None
        role = overrides.get(col, {}).get("role") or ("measure" if dt in ("integer", "real") and not is_id else "dimension")
        ftype = "quantitative" if role == "measure" else ("ordinal" if dt in ("integer", "date", "datetime") else "nominal")
        fields.append(Field(col, dt, role, ftype))
    return fields


@dataclass
class Table:
    path: Path
    fields: list = dc_field(default_factory=list)
    conn: str = ""
    object_id: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ref(self) -> str:
        return f"[{self.path.stem}#{self.path.suffix.lstrip('.')}]"

    def column(self, remote: str) -> "Field | None":
        return next((f for f in self.fields if f.remote.lower() == remote.strip().lower()), None)


def geo_kind(field: Field, override: str | None) -> str | None:
    kind = override or GEO_ALIASES.get(re.sub(r"[_\s]+", " ", (field.remote or field.name).strip().lower()))
    if not kind:
        return None
    if kind not in SEMANTIC_ROLES:
        raise SpecError(f"geo role {kind!r} for {field.name!r}: use one of {sorted(SEMANTIC_ROLES)}")
    numeric = field.datatype in ("integer", "real")
    if kind in ("latitude", "longitude") and not numeric:
        return None
    if kind not in ("latitude", "longitude") and numeric and kind != "zip":
        return None
    return kind


def load_model(paths: list[Path], separator: str = ",", overrides: dict | None = None) -> list[Table]:
    """Load CSVs as tables. Columns whose name an earlier table already uses get
    Tableau's local name "<column> (<file>.csv)"."""
    overrides = overrides or {}
    tables, used = [], set()
    for path in paths:
        table = Table(path)
        for f in load_fields(path, separator, overrides):
            f.remote, f.table = f.name, path.name
            if f.name.lower() in used:
                f.name = f"{f.name} ({path.name})"
            used.add(f.name.lower())
            ov = overrides.get(f.name) or overrides.get(f.remote) or {}
            f.geo = geo_kind(f, ov.get("geo"))
            if f.geo in ("latitude", "longitude"):
                f.role, f.type = "measure", "quantitative"
            table.fields.append(f)
        tables.append(table)
    return tables


# ------------------------------------------------------------- pill compiler
@dataclass(frozen=True)
class Instance:
    field: Field
    derivation: str
    prefix: str
    suffix: str        # nk | ok | qk

    @property
    def local(self) -> str:
        return f"[{self.prefix}:{self.field.name.replace(']', ']]')}:{self.suffix}]"

    @property
    def discrete(self) -> bool:
        return self.suffix in ("nk", "ok")

    @property
    def type(self) -> str:
        return {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}[self.suffix]


class Compiler:
    def __init__(self, spec: dict, base_dir: Path):
        self.spec = spec
        self.base_dir = base_dir
        ds = spec.get("datasource") or {}
        if "csv" in ds:
            entries = [ds["csv"]]
        elif ds.get("tables"):
            entries = [t if isinstance(t, str) else t["csv"] for t in ds["tables"]]
        else:
            raise SpecError("datasource needs 'csv' or 'tables'")
        self.separator = ds.get("separator", ",")
        self.tables = load_model([(base_dir / e).resolve() for e in entries], self.separator, ds.get("fields", {}))
        self.csv_path = self.tables[0].path
        self.links, self.joins = ds.get("relationships") or [], ds.get("joins") or []
        self.check_model()
        self.caption = ds.get("caption") or (self.csv_path.stem + ("+" if len(self.tables) > 1 else ""))
        self.fields = [f for t in self.tables for f in t.fields]
        self.by_name = {f.name.lower(): f for f in self.fields}
        for calc in spec.get("calculations", []):
            calc_id = "Calculation_" + str(int(hashlib.sha256(calc["name"].encode()).hexdigest(), 16))[:19]
            agg = bool(AGG_IN_FORMULA.search(calc["formula"]))
            role = calc.get("role") or ("measure" if agg or calc.get("datatype", "real") in ("real", "integer") else "dimension")
            f = Field(calc_id, calc.get("datatype", "real"), role,
                      "quantitative" if role == "measure" else "nominal", calc["formula"], calc["name"])
            self.fields.append(f)
            self.by_name[calc["name"].lower()] = f
        # Formulas may name other calculations by caption; Tableau stores internal names in the XML.
        calc_fields = [f for f in self.fields if f.formula is not None]
        by_caption = {f.caption: f.internal for f in calc_fields if f.caption}
        for f in calc_fields:
            f.formula = re.sub(r"\[((?:[^\]]|\]\])+)\]",
                               lambda m: by_caption.get(m.group(1).replace("]]", "]"), m.group(0)), f.formula)
        changed = True
        while changed:  # a calc that uses an aggregate calc is aggregate too
            changed = False
            agg_names = {g.internal for g in calc_fields if g.is_aggregate_calc}
            for f in calc_fields:
                if not f.is_aggregate_calc and any(n in f.formula for n in agg_names):
                    f.aggregate, changed = True, True
        for b in spec.get("bins", []):
            src = self.field(b["field"])
            if src.datatype not in ("integer", "real"):
                raise SpecError(f"bin on {b['field']!r}: needs a numeric field")
            name = b.get("name") or f"{src.caption or src.name} (bin)"
            f = Field(name, "integer", "dimension", "ordinal", src.internal, name, "bin", float(b["size"]))
            self.fields.append(f)
            self.by_name[name.lower()] = f
        for fname, kind in (spec.get("formats") or {}).items():
            if kind not in FORMATS:
                raise SpecError(f"format {kind!r} for {fname!r}: use one of {sorted(FORMATS)}")
            self.field(fname).default_format = FORMATS[kind]
        self.ds_name = "federated." + stable_id("ds", *(str(t.path) for t in self.tables))
        for t in self.tables:
            t.conn = "textscan." + stable_id("conn", str(t.path.parent))
            t.object_id = t.name + "_" + stable_id("obj", str(t.path), length=32).upper()

    def table(self, name: str, where: str) -> Table:
        key = name.strip().lower()
        t = next((t for t in self.tables if key in (t.name.lower(), t.path.stem.lower())), None)
        if t is None:
            raise SpecError(f"{where}: unknown table {name!r}; tables: {[t.name for t in self.tables]}")
        return t

    def check_model(self) -> None:
        """Validate relationships/joins: one key pair each, and every table connected."""
        if len(self.tables) == 1:
            if self.links or self.joins:
                raise SpecError("relationships/joins need two or more tables")
            return
        if self.links and self.joins:
            raise SpecError("use either relationships or joins, not both")
        if not (self.links or self.joins):
            raise SpecError("two or more tables need 'relationships' (recommended) or 'joins'")
        connected = {self.tables[0].name}
        for kind, items in (("relationship", self.links), ("join", self.joins)):
            for i, link in enumerate(items):
                where = f"{kind} {i + 1}"
                left, right = self.table(link["left"], where), self.table(link["right"], where)
                on = link.get("on") or []
                if len(on) != 1:
                    raise SpecError(f"{where}: give exactly one key pair in 'on' (multi-key links aren't supported yet)")
                lcol, rcol = on[0]
                if left.column(lcol) is None or right.column(rcol) is None:
                    raise SpecError(f"{where}: key {lcol!r}/{rcol!r} must be columns of {left.name}/{right.name}")
                if kind == "join" and link.get("type", "inner") not in ("inner", "left"):
                    raise SpecError(f"{where}: join type must be 'inner' or 'left'")
                if left.name not in connected or right.name in connected:
                    raise SpecError(f"{where}: link a table already in the model ({sorted(connected)}) "
                                    f"to a new one; got {left.name} -> {right.name}")
                connected.add(right.name)
        missing = [t.name for t in self.tables if t.name not in connected]
        if missing:
            raise SpecError(f"tables {missing} aren't linked to the model")

    def field(self, name: str) -> Field:
        f = self.by_name.get(name.strip().lower())
        if f is None:
            raise SpecError(f"unknown field {name!r}; known: {sorted(x.caption or x.name for x in self.fields)}")
        return f

    def pill(self, text: str) -> Instance:
        m = PILL.match(text)
        if m and (m.group(1).upper() in MEASURE_FUNCS | DATE_PARTS | DATE_TRUNCS):
            fn, f = m.group(1).upper(), self.field(m.group(2))
            if fn in MEASURE_FUNCS:
                d, p = MEASURE_FUNCS[fn]
                return Instance(f, d, p, "qk")
            if f.datatype not in ("date", "datetime"):
                raise SpecError(f"{fn}() needs a date field, {f.name!r} is {f.datatype}")
            if fn in DATE_PARTS:
                d, p = DATE_PARTS[fn]
                return Instance(f, d, p, "ok")
            d, p = DATE_TRUNCS[fn]
            return Instance(f, d, p, "qk")
        f = self.field(text)
        if f.formula is not None and f.is_aggregate_calc:
            return Instance(f, "User", "usr", "qk" if f.role == "measure" else "nk")
        if f.role == "measure":
            d, p = MEASURE_FUNCS["SUM"] if f.datatype in ("integer", "real") else MEASURE_FUNCS["COUNT"]
            return Instance(f, d, p, "qk")
        return Instance(f, "None", "none", "ok" if f.type == "ordinal" else "nk")

    def qualified(self, inst: Instance) -> str:
        return f"{bracket(self.ds_name)}.{inst.local}"

    def shelf(self, pills: list[str] | str | None, used: list[Instance], where: str) -> str:
        if not pills:
            return ""
        insts = [self.pill(p) for p in ([pills] if isinstance(pills, str) else pills)]
        used.extend(insts)
        discrete = [i for i in insts if i.discrete]
        continuous = [i for i in insts if not i.discrete]
        if discrete and continuous:
            raise SpecError(f"{where}: mixing discrete and continuous pills on one shelf is not supported yet")
        op = " / " if discrete else " + "
        exprs = [self.qualified(i) for i in insts]
        return exprs[0] if len(exprs) == 1 else "(" + op.join(exprs) + ")"

    # ------------------------------------------------------------ datasource
    def table_relation(self, t: Table) -> ET.Element:
        rel = ET.Element("relation", connection=t.conn, name=t.name, table=t.ref, type="table")
        cols = ET.SubElement(rel, "columns", {"character-set": "UTF-8", "header": "yes", "locale": "en_US",
                                              "separator": self.separator})
        for i, f in enumerate(t.fields):
            ET.SubElement(cols, "column", datatype=f.datatype, name=f.remote, ordinal=str(i))
        return rel

    def relation(self, parent: ET.Element) -> None:
        if len(self.tables) == 1:
            parent.append(self.table_relation(self.tables[0]))
        elif self.links:
            coll = ET.SubElement(parent, "relation", type="collection")
            for t in self.tables:
                coll.append(self.table_relation(t))
        else:
            tree = self.table_relation(self.tables[0])
            for j in self.joins:
                left, right = self.table(j["left"], "join"), self.table(j["right"], "join")
                lcol, rcol = j["on"][0]
                node = ET.Element("relation", join=j.get("type", "inner"), type="join")
                expr = ET.SubElement(ET.SubElement(node, "clause", type="join"), "expression", op="=")
                ET.SubElement(expr, "expression", op=f"[{left.name}].{bracket(left.column(lcol).remote)}")
                ET.SubElement(expr, "expression", op=f"[{right.name}].{bracket(right.column(rcol).remote)}")
                node.append(tree)
                node.append(self.table_relation(right))
                tree = node
            parent.append(tree)

    def column_element(self, parent: ET.Element, f: Field) -> None:
        attrs = {"datatype": f.datatype, "name": f.internal, "role": f.role, "type": f.type}
        if f.caption:
            attrs["caption"] = f.caption
        if f.default_format:
            attrs["default-format"] = f.default_format
        if f.semantic_role:
            attrs["semantic-role"] = f.semantic_role
        col = ET.SubElement(parent, "column", attrs)
        if f.calc_class == "bin":
            size = int(f.bin_size) if f.bin_size == int(f.bin_size) else f.bin_size
            ET.SubElement(col, "calculation", {"class": "bin", "decimals": "2", "formula": f.formula,
                                               "peg": "0", "size": str(size)})
        elif f.formula is not None:
            ET.SubElement(col, "calculation", {"class": "tableau", "formula": f.formula})

    def datasource(self, parent: ET.Element, directory: str | None) -> None:
        ds = ET.SubElement(parent, "datasource", caption=self.caption, inline="true", name=self.ds_name, version=VERSION)
        conn = ET.SubElement(ds, "connection", {"class": "federated"})
        named_conns = ET.SubElement(conn, "named-connections")
        for t in {t.conn: t for t in self.tables}.values():  # one text connection per CSV directory
            named = ET.SubElement(named_conns, "named-connection", caption=t.path.stem, name=t.conn)
            ET.SubElement(named, "connection", {"class": "textscan", "directory": directory or str(t.path.parent),
                                                "filename": t.name,
                                                "server": "", "workgroup-auth-mode": "as-is"})
        self.relation(conn)
        multi = len(self.tables) > 1
        if multi:
            cols = ET.SubElement(conn, "cols")
            for f in sorted((f for t in self.tables for f in t.fields), key=lambda f: f.internal):
                ET.SubElement(cols, "map", key=f.internal, value=f"[{f.table}].{bracket(f.remote)}")
        ET.SubElement(conn, "refresh", {"increment-key": "", "incremental-updates": "false"})
        records = ET.SubElement(conn, "metadata-records")
        for t in self.tables:
            object_id = self.tables[0].object_id if self.joins else t.object_id
            for i, f in enumerate(t.fields):
                rec = ET.SubElement(records, "metadata-record", {"class": "column"})
                for tag, text in (("remote-name", f.remote), ("remote-type", REMOTE_TYPE[f.datatype]),
                                  ("local-name", f.internal), ("parent-name", f"[{t.name}]"),
                                  ("remote-alias", f.remote), ("ordinal", str(i)), ("local-type", f.datatype),
                                  ("aggregation", DEFAULT_AGG[f.datatype]), ("contains-null", "true"),
                                  ("object-id", f"[{object_id}]")):
                    ET.SubElement(rec, tag).text = text
        ET.SubElement(ds, "aliases", enabled="yes")
        if self.links:
            for t in self.tables:
                ET.SubElement(ds, "column", caption=t.name, datatype="table", role="measure", type="quantitative",
                              name=f"[__tableau_internal_object_id__].[{t.object_id}]")
        for f in self.fields:
            self.column_element(ds, f)
        ET.SubElement(ds, "layout", {"dim-ordering": "alphabetic", "measure-ordering": "alphabetic",
                                     "show-structure": "true"})
        graph = ET.SubElement(ds, "object-graph")
        objects = ET.SubElement(graph, "objects")
        if self.links:
            for t in self.tables:
                obj = ET.SubElement(objects, "object", caption=t.name, id=t.object_id)
                ET.SubElement(obj, "properties", context="").append(self.table_relation(t))
            rels = ET.SubElement(graph, "relationships")
            for link in self.links:
                left, right = self.table(link["left"], "relationship"), self.table(link["right"], "relationship")
                lcol, rcol = link["on"][0]
                rel = ET.SubElement(rels, "relationship")
                expr = ET.SubElement(rel, "expression", op="=")
                ET.SubElement(expr, "expression", op=left.column(lcol).internal)
                ET.SubElement(expr, "expression", op=right.column(rcol).internal)
                ET.SubElement(rel, "first-end-point", {"object-id": left.object_id})
                ET.SubElement(rel, "second-end-point", {"object-id": right.object_id})
        else:
            first = self.tables[0]
            obj = ET.SubElement(objects, "object", caption=first.name, id=first.object_id)
            self.relation(ET.SubElement(obj, "properties", context=""))

    # ------------------------------------------------------------- worksheets
    def filters(self, view: ET.Element, ws: dict, used: list[Instance]) -> list[str]:
        sliced = []
        for flt in ws.get("filters", []):
            inst = self.pill(flt["field"])
            used.append(inst)
            q = self.qualified(inst)
            if "include" in flt or "exclude" in flt:
                values = flt.get("include") or flt.get("exclude")
                el = ET.SubElement(view, "filter", {"class": "categorical", "column": q})
                outer = ET.SubElement(el, "groupfilter", {
                    "function": "union" if "include" in flt else "except",
                    f"{{{USER_NS}}}ui-domain": "database",
                    f"{{{USER_NS}}}ui-enumeration": "inclusive" if "include" in flt else "exclusive",
                    f"{{{USER_NS}}}ui-marker": "enumerate"})
                target = outer
                if "exclude" in flt:
                    ET.SubElement(outer, "groupfilter", {"function": "level-members", "level": inst.local})
                    target = ET.SubElement(outer, "groupfilter", {"function": "union"})
                for v in values:
                    member = json.dumps(v) if isinstance(v, str) else str(v)
                    ET.SubElement(target, "groupfilter", {"function": "member", "level": inst.local, "member": member})
            elif "min" in flt or "max" in flt:
                el = ET.SubElement(view, "filter", {"class": "quantitative", "column": q,
                                                    "included-values": "in-range"})
                if "min" in flt:
                    ET.SubElement(el, "min").text = str(flt["min"])
                if "max" in flt:
                    ET.SubElement(el, "max").text = str(flt["max"])
            else:
                raise SpecError(f"filter on {flt['field']!r} needs include/exclude or min/max")
            sliced.append(q)
        return sliced

    def worksheet(self, parent: ET.Element, ws: dict, shared: list[tuple[str, int]] = ()) -> None:
        name = ws["name"]
        mark = ws.get("mark", "Automatic")
        if mark not in MARKS:
            raise SpecError(f"{name}: mark {mark!r} not in {sorted(MARKS)}")
        used: list[Instance] = []
        map_mode = ws.get("map")
        if map_mode and map_mode not in MAP_MODES:
            raise SpecError(f"{name}: map {map_mode!r} not in {sorted(MAP_MODES)}")
        if map_mode in ("filled", "symbol") and (ws.get("rows") or ws.get("cols")):
            raise SpecError(f"{name}: a {map_mode} map places Tableau's generated latitude/longitude itself; "
                            "leave rows and cols empty")
        rows = self.shelf(ws.get("rows"), used, f"{name}.rows")
        cols = self.shelf(ws.get("cols"), used, f"{name}.cols")
        enc: list[tuple[str, Instance]] = []
        geo_val = ws.get("geo")
        for p in ([geo_val] if isinstance(geo_val, str) else (geo_val or [])):
            inst = self.pill(p)
            if map_mode in ("filled", "symbol") and (not inst.field.semantic_role or inst.field.geo in ("latitude", "longitude")):
                raise SpecError(f"{name}: geo field {p!r} has no geographic role (country, state, city, county or zip)")
            used.append(inst)
            enc.append(("lod", inst))
        if map_mode in ("filled", "symbol") and not geo_val:
            raise SpecError(f"{name}: a {map_mode} map needs 'geo' with a geographic field")
        if map_mode in ("filled", "symbol"):
            rows = f"{bracket(self.ds_name)}.[Latitude (generated)]"
            cols = f"{bracket(self.ds_name)}.[Longitude (generated)]"
        if map_mode == "points":
            lat = [i for i in used if i.field.geo == "latitude"]
            lon = [i for i in used if i.field.geo == "longitude"]
            if not (lat and lon) or len(ws.get("rows") or []) != 1 or len(ws.get("cols") or []) != 1:
                raise SpecError(f"{name}: a points map needs a latitude measure on rows and a longitude measure on cols")
        for key, tag in ENCODINGS.items():
            val = ws.get(key)
            for p in ([val] if isinstance(val, str) else (val or [])):
                inst = self.pill(p)
                used.append(inst)
                enc.append((tag, inst))

        sheet = ET.SubElement(parent, "worksheet", name=name)
        table = ET.SubElement(sheet, "table")
        view = ET.SubElement(table, "view")
        ET.SubElement(ET.SubElement(view, "datasources"), "datasource", caption=self.caption, name=self.ds_name)
        if map_mode:
            ET.SubElement(ET.SubElement(view, "mapsources"), "mapsource", name="Tableau")
        deps = ET.SubElement(view, "datasource-dependencies", datasource=self.ds_name)
        sliced_holder = ET.Element("tmp")
        sliced = self.filters(sliced_holder, ws, used)
        for pill, group in shared:  # dashboard quick filters: all values, linked across sheets by filter-group
            inst = self.pill(pill)
            if not inst.discrete:
                raise SpecError(f"dashboard filter {pill!r} must be a dimension")
            used.append(inst)
            q = self.qualified(inst)
            el = ET.SubElement(sliced_holder, "filter", {"class": "categorical", "column": q, "filter-group": str(group)})
            ET.SubElement(el, "groupfilter", {"function": "level-members", "level": inst.local,
                                              f"{{{USER_NS}}}ui-enumeration": "all",
                                              f"{{{USER_NS}}}ui-marker": "enumerate"})
            if q not in sliced:
                sliced.append(q)
        seen_cols, seen_inst = set(), set()
        for inst in used:
            if inst.field.name not in seen_cols:
                seen_cols.add(inst.field.name)
                self.column_element(deps, inst.field)
        for inst in used:
            if inst.local not in seen_inst:
                seen_inst.add(inst.local)
                ET.SubElement(deps, "column-instance", column=inst.field.internal, derivation=inst.derivation,
                              name=inst.local, pivot="key", type=inst.type)
        for el in sliced_holder:
            view.append(el)
        sort = ws.get("sort")
        if sort:
            on, by = self.pill(sort["field"]), self.pill(sort["by"])
            if not on.discrete or by.discrete:
                raise SpecError(f"{name}: sort needs a discrete field sorted by a continuous one")
            for inst in (on, by):
                if inst.local not in seen_inst:
                    raise SpecError(f"{name}: sort field {sort['field']!r}/{sort['by']!r} must be used in the view")
            ET.SubElement(view, "sort", {"class": "computed", "column": self.qualified(on),
                                         "direction": "DESC" if sort.get("direction", "desc") == "desc" else "ASC",
                                         "using": self.qualified(by)})
        if sliced:
            slices = ET.SubElement(view, "slices")
            for q in sliced:
                ET.SubElement(slices, "column").text = q
        ET.SubElement(view, "aggregation", value="true")

        # Panes: one default pane, plus one per row measure when marks differ per measure (combo / dual axis).
        row_measures = [self.pill(p) for p in (ws.get("rows") or [])]
        row_measures = [i for i in row_measures if not i.discrete]
        marks = ws.get("marks")
        if marks and len(marks) != len(row_measures):
            raise SpecError(f"{name}: 'marks' needs one mark per measure on rows ({len(row_measures)})")
        for m in marks or []:
            if m not in MARKS:
                raise SpecError(f"{name}: mark {m!r} not in {sorted(MARKS)}")
        dual = bool(ws.get("dual_axis"))
        if dual and len(row_measures) != 2:
            raise SpecError(f"{name}: dual_axis needs exactly two measures on rows")
        enc_cols = [(tag, self.qualified(inst)) for tag, inst in enc]
        if map_mode == "filled":
            enc_cols.append(("geometry", f"{bracket(self.ds_name)}.[Geometry (generated)]"))
        if (marks or dual) and not any(tag == "color" for tag, _ in enc_cols):
            enc_cols.insert(0, ("color", f"{bracket(self.ds_name)}.{MEASURE_NAMES}"))
        reflines = []
        for i, r in enumerate(ws.get("reference_lines") or []):
            inst = self.pill(r["measure"])
            if inst.discrete or inst.local not in seen_inst:
                raise SpecError(f"{name}: reference line needs a measure used in the view, got {r['measure']!r}")
            formula = r.get("formula", "average")
            if formula not in REFLINE_FORMULAS:
                raise SpecError(f"{name}: reference line formula {formula!r} not in {sorted(REFLINE_FORMULAS)}")
            reflines.append((f"refline{i}", self.qualified(inst), formula))

        style = ET.SubElement(table, "style")
        if map_mode:
            ET.SubElement(ET.SubElement(style, "style-rule", element="map"), "format", attr="washout", value="0.0")
        if dual:
            rule = ET.SubElement(style, "style-rule", element="axis")
            ET.SubElement(rule, "encoding", {"attr": "space", "class": "0", "field": self.qualified(row_measures[1]),
                                             "field-type": "quantitative", "fold": "true", "scope": "rows",
                                             "synchronized": "true", "type": "space"})
        if reflines:
            rule = ET.SubElement(style, "style-rule", element="refline")
            for rid, _, _ in reflines:
                ET.SubElement(rule, "format", attr="fill-above", id=rid, value="#00000000")
                ET.SubElement(rule, "format", attr="fill-below", id=rid, value="#00000000")
        panes = ET.SubElement(table, "panes")
        pane_specs = [(None, mark)]
        if marks:
            pane_specs += [(inst, m) for inst, m in zip(row_measures, marks)]
        for idx, (axis_inst, pane_mark) in enumerate(pane_specs):
            attrs = {"selection-relaxation-option": "selection-relaxation-allow"}
            if axis_inst is not None:
                attrs = {"id": str(idx), **attrs, "y-axis-name": self.qualified(axis_inst)}
            pane = ET.SubElement(panes, "pane", attrs)
            ET.SubElement(ET.SubElement(pane, "view"), "breakdown", value="auto")
            ET.SubElement(pane, "mark", {"class": pane_mark})
            if enc_cols:
                encodings = ET.SubElement(pane, "encodings")
                for tag, col in enc_cols:
                    ET.SubElement(encodings, tag, column=col)
            pane_axes = {self.qualified(i) for i, _ in pane_specs[1:]}
            for rid, col, formula in reflines:
                # A line on a per-measure axis lives in that measure's pane; anything else in the default pane.
                home = axis_inst is not None and self.qualified(axis_inst) == col
                if home or (axis_inst is None and col not in pane_axes):
                    ET.SubElement(pane, "reference-line", {
                        "axis-column": col, "enable-instant-analytics": "true", "formula": formula, "id": rid,
                        "label-type": "automatic", "probability": "95", "scope": "per-table",
                        "value-column": col, "z-order": "1"})
        ET.SubElement(table, "rows").text = rows or None
        ET.SubElement(table, "cols").text = cols or None
        ET.SubElement(sheet, "simple-id", uuid=stable_uuid("ws", name))

    # ------------------------------------------------------------- dashboards
    def dashboard(self, parent: ET.Element, db: dict, sheet_names: set[str]) -> None:
        w, h = int(db.get("width", 1200)), int(db.get("height", 800))
        sheets = db["worksheets"]
        missing = [s for s in sheets if s not in sheet_names]
        if missing:
            raise SpecError(f"dashboard {db['name']!r} references unknown worksheets {missing}")
        filters = [self.pill(f) for f in db.get("filters") or []]
        el = ET.SubElement(parent, "dashboard", name=db["name"])
        ET.SubElement(el, "style")
        ET.SubElement(el, "size", maxheight=str(h), maxwidth=str(w), minheight=str(h), minwidth=str(w))
        if filters:  # a dashboard showing filter cards declares the fields it uses
            ET.SubElement(ET.SubElement(el, "datasources"), "datasource", caption=self.caption, name=self.ds_name)
            deps = ET.SubElement(el, "datasource-dependencies", datasource=self.ds_name)
            for inst in filters:
                self.column_element(deps, inst.field)
            for inst in filters:
                ET.SubElement(deps, "column-instance", column=inst.field.internal, derivation=inst.derivation,
                              name=inst.local, pivot="key", type=inst.type)
        zones = ET.SubElement(el, "zones")
        ids = iter(range(1, 10_000))
        root = ET.SubElement(zones, "zone", {"h": "100000", "id": str(next(ids)), "type-v2": "layout-basic",
                                             "w": "100000", "x": "0", "y": "0"})
        outer = ET.SubElement(root, "zone", {"h": "98000", "id": str(next(ids)), "param": "vert",
                                             "type-v2": "layout-flow", "w": "98400", "x": "800", "y": "1000"})
        y, left, width = 1000, 800, 98400
        title = db.get("title")
        if title:
            zone = ET.SubElement(outer, "zone", {"h": "6000", "id": str(next(ids)), "type-v2": "text",
                                                 "w": str(width), "x": str(left), "y": str(y)})
            run = ET.SubElement(ET.SubElement(zone, "formatted-text"), "run", bold="true", fontsize="16")
            run.text = title
            y += 6000
        if filters:
            row = ET.SubElement(outer, "zone", {"h": "7000", "id": str(next(ids)), "param": "horz",
                                                "type-v2": "layout-flow", "w": str(width), "x": str(left), "y": str(y)})
            fw = width // len(filters)
            for i, inst in enumerate(filters):
                ET.SubElement(row, "zone", {"h": "7000", "id": str(next(ids)), "mode": "checkdropdown",
                                            "name": sheets[0], "param": self.qualified(inst), "type-v2": "filter",
                                            "values": "relevant", "w": str(fw), "x": str(left + i * fw), "y": str(y)})
            y += 7000
        vertical = db.get("orientation", "vertical") == "vertical"
        body_h = 99000 - y
        flow = ET.SubElement(outer, "zone", {"h": str(body_h), "id": str(next(ids)),
                                             "param": "vert" if vertical else "horz", "type-v2": "layout-flow",
                                             "w": str(width), "x": str(left), "y": str(y)})
        n = len(sheets)
        for i, sheet in enumerate(sheets):
            if vertical:
                x, zy, zw, zh = left, y + i * body_h // n, width, body_h // n
            else:
                x, zy, zw, zh = left + i * width // n, y, width // n, body_h
            ET.SubElement(flow, "zone", {"h": str(zh), "id": str(next(ids)), "name": sheet, "w": str(zw),
                                         "x": str(x), "y": str(zy)})
        style = ET.SubElement(root, "zone-style")
        for attr, value in (("border-color", "#000000"), ("border-style", "none"), ("border-width", "0"),
                            ("margin", "8")):
            ET.SubElement(style, "format", attr=attr, value=value)
        ET.SubElement(el, "simple-id", uuid=stable_uuid("db", db["name"]))

    # ------------------------------------------------------------------ build
    def build(self, directory: str | None) -> ET.ElementTree:
        ET.register_namespace("user", USER_NS)
        wb = ET.Element("workbook", {"original-version": VERSION, "source-build": "0.0.0 (0000.0.0.0)",
                                     "source-platform": "mac", "version": VERSION})
        has_map = any(ws.get("map") for ws in self.spec.get("worksheets") or [])
        manifest = ET.SubElement(wb, "document-format-change-manifest")
        for flag in sorted(MANIFEST_FLAGS + (("MapboxVectorStylesAndLayers",) if has_map else ())):
            ET.SubElement(manifest, flag)
        prefs = ET.SubElement(wb, "preferences")
        ET.SubElement(prefs, "preference", name="ui.encoding.shelf.height", value="24")
        ET.SubElement(prefs, "preference", name="ui.shelf.height", value="26")
        self.datasource(ET.SubElement(wb, "datasources"), directory)
        if has_map:
            ET.SubElement(ET.SubElement(wb, "mapsources"), "mapsource", name="Tableau")
        sheets = self.spec.get("worksheets") or []
        if not sheets:
            raise SpecError("at least one worksheet is required")
        dbs = self.spec.get("dashboards") or []
        shared: dict[str, list[tuple[str, int]]] = {}
        group = 0
        for db in dbs:
            for f in db.get("filters") or []:
                group += 1
                for sheet in db["worksheets"]:
                    shared.setdefault(sheet, []).append((f, group))
        wss = ET.SubElement(wb, "worksheets")
        for ws in sheets:
            self.worksheet(wss, ws, shared.get(ws["name"], []))
        names = {ws["name"] for ws in sheets}
        if dbs:
            dbel = ET.SubElement(wb, "dashboards")
            for db in dbs:
                self.dashboard(dbel, db, names)
        windows = ET.SubElement(wb, "windows", {"source-height": "30"})
        for ws in sheets:
            win = ET.SubElement(windows, "window", {"class": "worksheet", "name": ws["name"]})
            cards = ET.SubElement(win, "cards")
            left = ET.SubElement(ET.SubElement(cards, "edge", name="left"), "strip", size="160")
            for card in ("pages", "filters", "marks"):
                ET.SubElement(left, "card", type=card)
            top = ET.SubElement(cards, "edge", name="top")
            for size, card in (("2147483647", "columns"), ("2147483647", "rows"), ("31", "title")):
                ET.SubElement(ET.SubElement(top, "strip", size=size), "card", type=card)
            ET.SubElement(win, "simple-id", uuid=stable_uuid("win", ws["name"]))
        for i, db in enumerate(dbs):
            attrs = {"class": "dashboard", "name": db["name"]}
            if i == 0:
                attrs["maximized"] = "true"
            win = ET.SubElement(windows, "window", attrs)
            vps = ET.SubElement(win, "viewpoints")
            for s in db["worksheets"]:
                ET.SubElement(vps, "viewpoint", name=s)
            ET.SubElement(win, "active", id="-1")
            ET.SubElement(win, "simple-id", uuid=stable_uuid("dbwin", db["name"]))
        if not dbs:
            windows[0].set("maximized", "true")
        tree = ET.ElementTree(wb)
        ET.indent(tree, space="  ")
        return tree


def compile_spec(spec_path: Path, out: Path) -> Path:
    spec = json.loads(spec_path.read_text())
    comp = Compiler(spec, spec_path.parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".twbx":
        tree = comp.build(directory="Data")
        twb_name = out.with_suffix(".twb").name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(twb_name, ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True))
            for t in comp.tables:
                z.write(t.path, f"Data/{t.name}")
    else:
        comp.build(directory=None).write(out, encoding="utf-8", xml_declaration=True)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("spec")
    p.add_argument("-o", "--out", required=True, help=".twb or .twbx")
    a = p.parse_args(argv)
    try:
        print(compile_spec(Path(a.spec), Path(a.out)))
    except SpecError as e:
        print(f"spec error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
