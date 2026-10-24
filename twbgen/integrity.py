"""Semantic integrity checks that no XML schema can express.

A workbook can be schema-valid and still break in Tableau when a reference
points at nothing. These checks run on any .twb (generated or Desktop-saved)
and matter most when a model edits an existing workbook:

- every field referenced on rows/cols, encodings, filters, sorts, reference
  lines and dashboard filter cards resolves to a column-instance the sheet
  (or dashboard) declares, and every column-instance resolves to a column;
- every worksheet filter has a matching slice;
- worksheets/dashboards and windows correspond one-to-one;
- dashboard zones name real worksheets;
- calculation formulas reference fields that exist, by internal name.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, also under `python -I`
from normalize import read_twb, resolve_fcp_name  # noqa: E402

# [datasource].[field] or [datasource].[object].[field]; field names escape "]" as "]]".
QUALIFIED = re.compile(r"\[((?:[^\]]|\]\])+)\]((?:\.\[(?:[^\]]|\]\])+\])+)")
BUILTINS = {"[:Measure Names]", "[Multiple Values]", "[:Measure Values]", "[Number of Records]",
            "[Latitude (generated)]", "[Longitude (generated)]", "[Geometry (generated)]"}


def _builtin(name: str) -> bool:
    return name in BUILTINS or name.startswith("[__tableau_internal_object_id__]")


def _strip_fcp(root: ET.Element) -> ET.Element:
    """Read the document the way a current reader would (true-branch FCP wrappers kept)."""
    for e in list(root.iter()):
        for child in list(e):
            bare = resolve_fcp_name(child.tag)
            if bare is None:
                e.remove(child)
            else:
                child.tag = bare
    return root


def _refs(text: str | None) -> list[tuple[str, str]]:
    """(datasource name, bracketed local name) for each qualified reference."""
    return [(m.group(1).replace("]]", "]"), m.group(2)[1:]) for m in QUALIFIED.finditer(text or "")]


def _names(container: ET.Element) -> set[str]:
    """Field names a datasource (or dependencies block) defines: columns, groups, raw metadata columns."""
    names = {c.get("name") for c in container.findall("column")} | {g.get("name") for g in container.findall("group")}
    names |= {r.text for r in container.findall("connection/metadata-records/metadata-record/local-name")}
    return {n for n in names if n}


def check(root: ET.Element) -> list[str]:
    root = _strip_fcp(root)
    issues: list[str] = []
    datasources = {ds.get("name"): ds for ds in root.findall("datasources/datasource")}
    ds_columns = {name: _names(ds) for name, ds in datasources.items()}
    captions = {name: {c.get("caption"): c.get("name") for c in ds.findall("column") if c.get("caption")}
                for name, ds in datasources.items()}

    def declared(view: ET.Element) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for deps in view.findall("datasource-dependencies"):
            ds = deps.get("datasource")
            cols = _names(deps) | ds_columns.get(ds, set())
            insts = set(cols)  # bare columns/groups can be referenced directly (e.g. action filters)
            for ci in deps.findall("column-instance"):
                insts.add(ci.get("name"))
                if ci.get("column") not in cols and not _builtin(ci.get("column", "")):
                    issues.append(f"{where}: column-instance {ci.get('name')} points at missing column {ci.get('column')}")
            out[ds] = insts
        return out

    worksheets = {ws.get("name"): ws for ws in root.findall("worksheets/worksheet")}
    for name, ws in worksheets.items():
        where = f"worksheet {name!r}"
        view = ws.find("table/view")
        if view is None:
            continue
        for d in view.findall("datasources/datasource"):
            if d.get("name") not in datasources and d.get("name") != "Parameters":
                issues.append(f"{where}: uses undefined datasource {d.get('name')!r}")
        known = declared(view)

        def resolve(ref: tuple[str, str], context: str) -> None:
            ds, local = ref
            if _builtin(local) or ds == "Parameters":
                return
            if ds not in known:
                issues.append(f"{where}: {context} references datasource {ds!r} the sheet doesn't declare")
            elif local not in known[ds]:
                issues.append(f"{where}: {context} references {local}, which the sheet doesn't declare")

        table = ws.find("table")
        for shelf in ("rows", "cols"):
            for ref in _refs(table.findtext(shelf)):
                resolve(ref, shelf)
        for pane in table.iter("pane"):
            for e in pane.find("encodings") if pane.find("encodings") is not None else []:
                for ref in _refs(e.get("column")):
                    resolve(ref, f"{e.tag} encoding")
            for rl in pane.findall("reference-line"):
                for attr in ("axis-column", "value-column"):
                    for ref in _refs(rl.get(attr)):
                        resolve(ref, "reference line")
        slices = {c.text for c in view.findall("slices/column")}
        for f in view.findall("filter"):
            for ref in _refs(f.get("column")):
                resolve(ref, "filter")
            if f.get("column") not in slices and f.get("kind") != "hide":
                issues.append(f"{where}: filter on {f.get('column')} has no matching <slices> entry")
        for s in view.findall("sort"):
            for attr in ("column", "using"):
                for ref in _refs(s.get(attr)):
                    resolve(ref, "sort")

    dashboards = {db.get("name"): db for db in root.findall("dashboards/dashboard")}
    for name, db in dashboards.items():
        where = f"dashboard {name!r}"
        known = declared(db)
        for zone in db.iter("zone"):
            kind, zname = zone.get("type-v2") or zone.get("type"), zone.get("name")
            if zname and kind in (None, "filter") and zname not in worksheets:
                issues.append(f"{where}: zone names missing worksheet {zname!r}")
            if kind == "filter":
                for ds, local in _refs(zone.get("param")):
                    if local not in known.get(ds, set()):
                        issues.append(f"{where}: filter card field {local} isn't declared by the dashboard")

    windows = {(w.get("class"), w.get("name")) for w in root.findall("windows/window")}
    for name in worksheets:
        if ("worksheet", name) not in windows:
            issues.append(f"worksheet {name!r} has no window")
    for name in dashboards:
        if ("dashboard", name) not in windows:
            issues.append(f"dashboard {name!r} has no window")
    for cls, name in windows:
        if cls == "worksheet" and name not in worksheets:
            issues.append(f"window for missing worksheet {name!r}")
        if cls == "dashboard" and name not in dashboards:
            issues.append(f"window for missing dashboard {name!r}")

    for dsname, ds in datasources.items():
        names = ds_columns[dsname]
        for col in ds.findall("column"):
            calc = col.find("calculation")
            if calc is None or calc.get("class") != "tableau":
                continue
            formula = re.sub(r"//[^\r\n]*", " ", calc.get("formula") or "")              # comments
            formula = re.sub(r"\"[^\"]*\"|'[^']*'", " ", formula)                          # string literals
            formula = re.sub(r"\[Parameters\]\.\[(?:[^\]]|\]\])+\]", " ", formula)
            for ref in re.findall(r"\[((?:[^\]]|\]\])+)\]", formula):
                bracketed = f"[{ref}]"
                if bracketed in names or ref.startswith("Parameters"):
                    continue
                label = col.get("caption") or col.get("name")
                if ref in captions[dsname]:
                    issues.append(f"calculation {label!r}: refers to [{ref}] by caption; "
                                  f"Tableau formulas use the internal name {captions[dsname][ref]}")
                else:
                    issues.append(f"calculation {label!r}: references unknown field [{ref}]")
    return issues


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb", nargs="+")
    a = p.parse_args(argv)
    bad = 0
    for path in a.twb:
        issues = check(read_twb(path))
        print(f"{'OK    ' if not issues else 'ISSUES'} {path}")
        for i in issues[:30]:
            print("   ", i)
        bad += bool(issues)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
