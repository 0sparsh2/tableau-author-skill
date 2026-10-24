"""Deterministic best-practice checks over describe() output.

Deterministic "facts" first, then an LLM turns them into language or edits
(the same split Tableau's documentation describes for Pulse insights). Each finding names the sheet,
the rule, and a concrete suggested spec change, so a model can apply it and
recompile with spec_to_twb.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, also under `python -I`
from describe import describe_file  # noqa: E402

DEFAULT_SHEET = re.compile(r"^(Sheet|Dashboard|Story) \d+$")
COPY_NAME = re.compile(r"\(copy( \d+)?\)$")
AGG = re.compile(r"^[A-Z_]+\(")


def pills(shelf: dict | None) -> list[str]:
    return (shelf or {}).get("pills", [])


def lint(desc: dict) -> list[dict]:
    out: list[dict] = []

    def add(rule: str, target: str, message: str, suggestion: str, severity: str = "warning") -> None:
        out.append({"rule": rule, "severity": severity, "target": target, "message": message,
                    "suggestion": suggestion})

    used_text = json.dumps(desc["worksheets"])
    in_dashboards = {s for d in desc["dashboards"] for s in d["worksheets"]}

    for ws in desc["worksheets"]:
        name, rows, cols = ws["name"], pills(ws["rows"]), pills(ws["cols"])
        marks = {p.get("mark") for p in ws["panes"]}
        if DEFAULT_SHEET.match(name):
            add("default-name", name, "Sheet still has a default name.",
                "Rename to the question it answers, e.g. 'Sales by Region'.")
        if not rows and not cols and not any(len(p) > 1 for p in ws["panes"]):
            add("empty-sheet", name, "Worksheet has no fields on rows, columns or marks.",
                "Delete it or build the intended view.", "info")
            continue
        if "Pie" in marks:
            add("pie-chart", name, "Pie charts make part-to-whole comparisons hard to read beyond 3-4 slices.",
                "Use mark 'Bar' with the dimension on rows and the measure on cols, sorted descending.")
        for shelf_name, shelf in (("rows", ws["rows"]), ("cols", ws["cols"])):
            if shelf and shelf.get("operator") == "concat" and len(shelf["pills"]) > 3:
                add("many-measures", name, f"{len(shelf['pills'])} measures stacked on {shelf_name}.",
                    "Split into small multiples or keep the 2-3 measures that answer the question.")
        if all(AGG.match(p) for p in rows + cols) and rows and cols and not any(
                p.get("detail") or p.get("color") for p in ws["panes"]):
            add("single-mark", name, "Both axes are aggregates with no dimension, so the view draws one mark.",
                "Add a dimension to detail or color (e.g. the entity you want to compare).")
        for pane in ws["panes"]:
            for d in pane.get("detail", []):
                if re.search(r"(^|\s)id$", d, re.I):
                    add("row-level-detail", name, f"'{d}' on detail draws one mark per record.",
                        "Confirm row-level detail is intended; otherwise aggregate to a coarser dimension.",
                        "info")
        if len(ws["panes"]) == 1 and ws["panes"][0].get("mark") == "Automatic":
            add("automatic-mark", name, "Mark type is Automatic.",
                "Pin an explicit mark (Bar, Line, Circle...) so the view doesn't change as fields move.", "info")
        if not any("tooltip" in p for p in ws["panes"]) and (rows or cols):
            add("default-tooltip", name, "No tooltip fields; viewers only see the encoded fields.",
                "Add the supporting measure(s) a viewer would ask about next to 'tooltip'.", "info")
        if desc["dashboards"] and name not in in_dashboards:
            add("orphan-sheet", name, "Worksheet is not on any dashboard.",
                "Add it to a dashboard or hide it before publishing.", "info")

    for ds in desc["datasources"]:
        for calc in ds["calculations"]:
            label = calc["name"]
            if label.startswith("Calculation_"):
                add("unnamed-calc", label, "Calculated field has no caption.", "Give it a business name.")
            if COPY_NAME.search(label):
                add("copied-calc", label, "Calculated field is a '(copy)'.",
                    "Merge with the original or rename to say how it differs.")
            if label not in used_text:
                add("unused-calc", label, "Calculated field isn't used by any worksheet.",
                    "Remove it or use it.", "info")
    for db in desc["dashboards"]:
        n = len(db["worksheets"])
        if n > 6:
            add("busy-dashboard", db["name"], f"{n} views on one dashboard.",
                "Keep 2-4 primary views; move detail to a drill-through dashboard.")
        size = db.get("size") or {}
        if not size.get("maxwidth"):
            add("auto-size", db["name"], "Dashboard has automatic size; layout shifts across screens.",
                "Set a fixed size (e.g. 1200x800) or a range.", "info")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb")
    a = p.parse_args(argv)
    findings = lint(describe_file(a.twb))
    print(json.dumps(findings, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
