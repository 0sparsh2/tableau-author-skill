"""Validate a .twb the way an installed Tableau Desktop does.

Desktop does not validate against the published XSD. It ships its own TWB
schema with feature-flag preprocessor directives (`<!--?IF Flag -->`,
`<!--?ELSE -->`, `<!--?END -->`) and turns flags on from the workbook's
`<document-format-change-manifest>`. Forward-compatibility wrappers in the
document (`_.fcp.Flag.true...tag`) are resolved with the same flags.
This tool reproduces that pipeline locally:

    python3 desktop_check.py --extract-from "/Applications/Tableau Desktop … .app"
    python3 desktop_check.py workbook.twb [more.twb …]

The extracted schema is Tableau's file from *your* installation; it is cached
in xsd/desktop-local/ (git-ignored) and is not for redistribution.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "xsd" / "desktop-local"
RCC_TOOL = HERE / "extract_qt_rcc.py"
SCHEMAS = {"twb": "TQRC_FILEFORMAT_TWB_XSD", "user": "TQRC_FILEFORMAT_USER_XSD", "xml": "TQRC_FILEFORMAT_XML_XSD"}
DIRECTIVE = re.compile(r"<!--\?(IF\s+(!?)(\w+)|ELSE|END)\s*-->")
FCP = re.compile(r"_\.fcp\.([A-Za-z0-9_]+)\.(true|false)\.\.\.")


def extract(app: Path) -> None:
    rcc = app / "Contents" / "Resources" / "tablangres.rcc"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-I", str(RCC_TOOL), str(rcc), tmp], check=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        for key, name in SCHEMAS.items():
            (CACHE / f"{name}.xsd").write_bytes((Path(tmp) / name).read_bytes())
    (CACHE / ".gitignore").write_text("*\n")
    (CACHE / "SOURCE.txt").write_text(f"Extracted from {rcc}\nTableau's file; local validation only, do not redistribute.\n")
    print(f"cached Desktop schemas in {CACHE}")


def preprocess(text: str, flags: set[str]) -> str:
    """Resolve nested ?IF/?ELSE/?END blocks for the given enabled flags."""
    out, stack, pos = [], [], 0  # stack of [cond_true, in_else, parent_emitting]
    emitting = True
    for m in DIRECTIVE.finditer(text):
        if emitting:
            out.append(text[pos:m.start()])
        pos = m.end()
        if m.group(1).startswith("IF"):
            cond = (m.group(3) in flags) != (m.group(2) == "!")
            stack.append([cond, False, emitting])
            emitting = emitting and cond
        elif m.group(1) == "ELSE":
            top = stack[-1]
            top[1] = True
            emitting = top[2] and not top[0]
        else:  # END
            emitting = stack.pop()[2]
    if emitting:
        out.append(text[pos:])
    if stack:
        raise ValueError("unbalanced schema directives")
    return "".join(out)


def manifest_flags(root: ET.Element) -> tuple[set[str], bool]:
    manifest = root.find("document-format-change-manifest")
    tags = {FCP.sub("", c.tag) for c in (manifest if manifest is not None else [])}
    by_version = "ManifestByVersion" in tags
    return tags - {"ManifestByVersion"}, by_version


def resolve_fcp(elem: ET.Element, flags: set[str]) -> None:
    def keep(name: str) -> str | None:
        for flag, branch in FCP.findall(name):
            if (flag in flags) != (branch == "true"):
                return None
        return FCP.sub("", name)

    for child in list(elem):
        bare = keep(child.tag)
        if bare is None:
            elem.remove(child)
            continue
        child.tag = bare
        resolve_fcp(child, flags)
    for k in list(elem.attrib):
        v = elem.attrib.pop(k)
        bare = keep(k)
        if bare is not None:
            elem.attrib[bare] = v


def check(twb: Path) -> tuple[bool, list[str], set[str]]:
    if not (CACHE / f"{SCHEMAS['twb']}.xsd").exists():
        raise SystemExit("run --extract-from <Tableau .app> first")
    ET.register_namespace("user", "http://www.tableausoftware.com/xml/user")
    tree = ET.parse(twb)
    flags, by_version = manifest_flags(tree.getroot())
    issues = []
    if by_version:
        issues.append("note: <ManifestByVersion/> makes Desktop 2026.3 enable in-development format flags "
                      "(e.g. MarkTypeAttribute, SheetNumTstAddOnly); emit an explicit feature manifest instead")
    resolve_fcp(tree.getroot(), flags)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for key, name in SCHEMAS.items():
            text = (CACHE / f"{name}.xsd").read_text(encoding="utf-8")
            (tmp / f"{key}.xsd").write_text(preprocess(text, flags), encoding="utf-8")
        main = (tmp / "twb.xsd").read_text(encoding="utf-8")
        main = main.replace('<xs:import namespace="http://www.tableausoftware.com/xml/user"/>',
                            '<xs:import namespace="http://www.tableausoftware.com/xml/user" schemaLocation="user.xsd"/>')
        main = main.replace('<xs:import namespace="http://www.w3.org/XML/1998/namespace"/>',
                            '<xs:import namespace="http://www.w3.org/XML/1998/namespace" schemaLocation="xml.xsd"/>')
        (tmp / "twb.xsd").write_text(main, encoding="utf-8")
        doc = tmp / "doc.twb"
        tree.write(doc, encoding="utf-8", xml_declaration=True)
        proc = subprocess.run(["xmllint", "--noout", "--schema", str(tmp / "twb.xsd"), str(doc)],
                              capture_output=True, text=True)
    lines = [ln.split("doc.twb:", 1)[-1] for ln in proc.stderr.splitlines()
             if ln.strip() and not ln.endswith(("validates", "fails to validate"))]
    return proc.returncode == 0 and not by_version, issues + lines, flags


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb", nargs="*")
    p.add_argument("--extract-from", type=Path, help="path to the Tableau Desktop .app")
    a = p.parse_args(argv)
    if a.extract_from:
        extract(a.extract_from)
    failed = 0
    for path in a.twb:
        ok, issues, flags = check(Path(path))
        print(f"{'DESKTOP-VALID  ' if ok else 'DESKTOP-INVALID'} {path}  (flags: {', '.join(sorted(flags)) or 'none'})")
        for issue in issues[:25]:
            print("   ", issue[:300])
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
