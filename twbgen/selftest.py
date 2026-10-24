"""End-to-end check: compile the example spec, validate against the official XSD,
round-trip through describe(), lint, and confirm bad input is rejected.

Optional: pass Desktop-saved .twb files to also normalize + validate them.
"""
from __future__ import annotations

import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from describe import describe_file  # noqa: E402
from lint import lint  # noqa: E402
from normalize import normalize  # noqa: E402
from spec_to_twb import SpecError, compile_spec  # noqa: E402
from validate import validate  # noqa: E402
import desktop_check  # noqa: E402
import integrity  # noqa: E402
from author import build_and_check  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    return ok


def main(extra: list[str]) -> int:
    results = []
    spec_path = HERE / "examples" / "overview.spec.json"
    spec = json.loads(spec_path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        twb = compile_spec(spec_path, tmp / "overview.twb")
        if (desktop_check.CACHE / f"{desktop_check.SCHEMAS['twb']}.xsd").exists():
            ok, issues, _ = desktop_check.check(twb)
            results.append(check("generated .twb passes Tableau Desktop's own schema + manifest flags", ok,
                                 "; ".join(issues[:3])))
        else:
            print("SKIP  Desktop schema not cached (run desktop_check.py --extract-from <Tableau .app>)")

        desc = describe_file(str(twb))
        got = {w["name"]: w for w in desc["worksheets"]}
        same = True
        for ws in spec["worksheets"]:
            d = got.get(ws["name"])
            for shelf in ("rows", "cols"):
                want = ws.get(shelf) or []
                want = [want] if isinstance(want, str) else want
                have = (d or {}).get(shelf) and d[shelf]["pills"] or []
                same &= want == have
            same &= (d or {}).get("panes", [{}])[0].get("mark") == ws.get("mark", "Automatic")
        results.append(check("spec -> twb -> describe round-trips shelves and marks", same))
        results.append(check("lint runs", isinstance(lint(desc), list)))

        bad = dict(spec, worksheets=[dict(spec["worksheets"][0], rows=["Region", "SUM(Sales)"])])
        bad_path = HERE / "examples" / ".bad.spec.json"
        bad_path.write_text(json.dumps(bad))
        try:
            compile_spec(bad_path, tmp / "bad.twb")
            results.append(check("mixed discrete/continuous shelf is rejected", False))
        except SpecError:
            results.append(check("mixed discrete/continuous shelf is rejected", True))
        finally:
            bad_path.unlink()

        feat = compile_spec(HERE / "examples" / "features.spec.json", tmp / "features.twb")
        issues = integrity.check(ET.parse(feat).getroot())
        if (desktop_check.CACHE / f"{desktop_check.SCHEMAS['twb']}.xsd").exists():
            ok, more, _ = desktop_check.check(feat)
            issues += [] if ok else more
        results.append(check("feature spec (combo/dual axis, reference line, histogram, formats, "
                             "dashboard title + filters) passes integrity and Desktop schema",
                             not issues, "; ".join(issues[:3])))

        dangling = tmp / "dangling.twb"
        dangling.write_text(twb.read_text().replace("[none:Region:nk]</rows>", "[none:Regoin:nk]</rows>"))
        found = integrity.check(ET.parse(dangling).getroot())
        results.append(check("integrity catches a shelf reference to an undeclared field",
                             any("Regoin" in i for i in found)))

        for spec_name in ("multi/relationships", "multi/joins", "multi/maps", "multi/points", "multi/parameters",
                          "actions"):
            built = compile_spec(HERE / "examples" / f"{spec_name}.spec.json", tmp / f"{spec_name.replace('/', '_')}.twb")
            issues = integrity.check(ET.parse(built).getroot())
            if (desktop_check.CACHE / f"{desktop_check.SCHEMAS['twb']}.xsd").exists():
                ok, more, _ = desktop_check.check(built)
                issues += [] if ok else more
            results.append(check(f"{spec_name} spec passes integrity and Desktop schema", not issues,
                                 "; ".join(issues[:3])))

        datasets = json.loads((HERE / "examples" / "plans" / "datasets.json").read_text())
        plans = sorted(p for p in (HERE / "examples" / "plans").glob("*.json") if p.stem in datasets)
        failed_plans = []
        for plan in plans:
            csvs = [HERE / "examples" / c for c in datasets[plan.stem]]
            errors, _ = build_and_check(json.loads(plan.read_text()), csvs, tmp / f"{plan.stem}.twbx")
            if errors:
                failed_plans.append(f"{plan.stem}: {errors[0]}")
        results.append(check(f"all {len(plans)} example ChartSpec plans build and pass every check",
                             not failed_plans, "; ".join(failed_plans[:3])))

        broken = tmp / "broken.twb"
        broken.write_text(twb.read_text().replace('class="Bar"', 'class="Barchart"'))
        results.append(check("published XSD catches an invalid mark class", not validate(broken)[0]))

        for i, path in enumerate(extra):
            tree = ET.parse(path)
            normalize(tree.getroot())
            out = tmp / f"normalized_{i}.twb"
            tree.write(out, encoding="utf-8", xml_declaration=True)
            ok, issues = validate(out)
            results.append(check(f"normalized {Path(path).name} validates (published XSD, analysis form)", ok,
                                 "; ".join(issues[:3])))
            if (desktop_check.CACHE / f"{desktop_check.SCHEMAS['twb']}.xsd").exists():
                ok, issues, _ = desktop_check.check(Path(path))
                results.append(check(f"original {Path(path).name} passes Desktop schema", ok, "; ".join(issues[:3])))
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
