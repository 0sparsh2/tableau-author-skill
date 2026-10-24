"""Build the grounding context an authoring LLM gets about a data source.

Field names, data types, roles, sample values and ranges: the same kind of
metadata Tableau's documentation says Tableau Agent indexes (field name, data
type, description, sample values). Output is compact JSON meant to sit in the
model's prompt.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, also under `python -I`
from spec_to_twb import load_model  # noqa: E402


def profile_field(f, rows: list[dict], max_values: int, descriptions: dict | None) -> dict:
    col = [r[f.remote] for r in rows if r.get(f.remote, "") != ""]
    item = {"name": f.name, "datatype": f.datatype, "role": f.role, "nulls": len(rows) - len(col)}
    if f.geo:
        item["geo"] = f.geo
    if descriptions and f.name in descriptions:
        item["description"] = descriptions[f.name][:150]  # keep descriptions short (Tableau Agent indexes 150 chars)
    distinct = Counter(col)
    item["distinct"] = len(distinct)
    if f.datatype in ("integer", "real") and f.role == "measure":
        nums = [float(v) for v in col]
        item.update(min=min(nums), max=max(nums), mean=round(statistics.fmean(nums), 2),
                    has_negative=any(n < 0 for n in nums))
    elif f.datatype in ("date", "datetime"):
        item.update(min=min(col), max=max(col))
    else:
        item["values"] = [v for v, _ in distinct.most_common(max_values)]
        item["values_truncated"] = len(distinct) > max_values
    return item


def profile(csv_paths: Path | list[Path], separator: str = ",", max_values: int = 25,
            descriptions: dict | None = None) -> dict:
    """Profile one CSV, or several as tables of one data model (field names as the compiler sees them)."""
    paths = [csv_paths] if isinstance(csv_paths, Path) else list(csv_paths)
    tables = load_model(paths, separator)
    out = []
    for t in tables:
        with t.path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh, delimiter=separator))
        out.append({"table": t.name, "rows": len(rows),
                    "fields": [profile_field(f, rows, max_values, descriptions) for f in t.fields]})
    if len(out) == 1:
        return {"datasource": out[0]["table"], "rows": out[0]["rows"], "fields": out[0]["fields"]}
    return {"tables": out,
            "note": "Link the tables in the plan's data_model; a column already used by an earlier table "
                    "appears as '<column> (<file>.csv)'."}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv", nargs="+", help="one CSV, or several tables of one data model")
    p.add_argument("--separator", default=",")
    p.add_argument("--max-values", type=int, default=25)
    a = p.parse_args(argv)
    print(json.dumps(profile([Path(c) for c in a.csv], a.separator, a.max_values), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
