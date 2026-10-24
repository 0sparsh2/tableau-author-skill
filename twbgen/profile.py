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
from spec_to_twb import load_fields  # noqa: E402


def profile(csv_path: Path, separator: str = ",", max_values: int = 25, descriptions: dict | None = None) -> dict:
    fields = load_fields(csv_path, separator, {})
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=separator))
    out = []
    for f in fields:
        col = [r[f.name] for r in rows if r.get(f.name, "") != ""]
        item = {"name": f.name, "datatype": f.datatype, "role": f.role, "nulls": len(rows) - len(col)}
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
        out.append(item)
    return {"datasource": csv_path.name, "rows": len(rows), "fields": out}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv")
    p.add_argument("--separator", default=",")
    p.add_argument("--max-values", type=int, default=25)
    a = p.parse_args(argv)
    print(json.dumps(profile(Path(a.csv), a.separator, a.max_values), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
