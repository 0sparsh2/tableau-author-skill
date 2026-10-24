"""Normalize a Desktop-saved .twb into the canonical form the official XSD describes.

Tableau Desktop writes forward-compatibility ("FCP") wrappers such as
``<_.fcp.ObjectModelTableType.true...column>``: an element/attribute that only
applies when the reading build supports a feature flag. A current reader
supports every flag, so we keep ``.true...`` branches (renamed to the bare tag)
and drop ``.false...`` branches. We then pin the version and replace the
feature manifest with ``<ManifestByVersion />`` as tableau-document-schemas
recommends for direct authoring.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

FCP = re.compile(r"_\.fcp\.([A-Za-z0-9_]+)\.(true|false)\.\.\.")
DEFAULT_VERSION = "26.2"
# Workbook children that the 2026.x XSD orders before the (required) <explain-data>.
BEFORE_EXPLAIN_DATA = ("windows", "datagraph", "thumbnails", "external", "referenced-extensions")


def read_twb(path: str | Path) -> ET.Element:
    """Parse a .twb, or the workbook inside a packaged .twbx."""
    path = Path(path)
    if path.suffix.lower() == ".twbx":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".twb") and "/" not in n.strip("/")]
            if not names:
                raise ValueError(f"no .twb inside {path}")
            return ET.fromstring(z.read(names[0]))
    return ET.parse(path).getroot()


def resolve_fcp_name(name: str) -> str | None:
    """Return the bare name if every FCP condition is a true-branch, else None (drop)."""
    if any(branch == "false" for _, branch in FCP.findall(name)):
        return None
    return FCP.sub("", name)


def _resolve(elem: ET.Element) -> None:
    for child in list(elem):
        bare = resolve_fcp_name(child.tag)
        if bare is None:
            elem.remove(child)
            continue
        child.tag = bare
        _resolve(child)
    for key in list(elem.attrib):
        bare = resolve_fcp_name(key)
        value = elem.attrib.pop(key)
        if bare is not None:
            elem.attrib[bare] = value


def normalize(root: ET.Element, version: str = DEFAULT_VERSION) -> ET.Element:
    _resolve(root)
    root.set("version", version)
    root.set("original-version", version)
    root.set("source-build", root.get("source-build") or "0.0.0 (0000.0.0.0)")
    manifest = root.find("document-format-change-manifest")
    if manifest is None:
        manifest = ET.Element("document-format-change-manifest")
        root.insert(0, manifest)
    for child in list(manifest):
        manifest.remove(child)
    ET.SubElement(manifest, "ManifestByVersion")
    ensure_explain_data(root)
    return root


def ensure_explain_data(root: ET.Element) -> None:
    """2026.x builds always serialize <explain-data>; older files omit it."""
    if root.find("explain-data") is not None:
        return
    explain = ET.Element("explain-data", {"enabled-for-viewer": "true", "extreme-values-enabled-for-all": "true"})
    ET.SubElement(explain, "explanation-types")
    kids = list(root)
    anchor = max((i for i, k in enumerate(kids) if k.tag in BEFORE_EXPLAIN_DATA), default=len(kids) - 1)
    root.insert(anchor + 1, explain)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--version", default=DEFAULT_VERSION)
    a = p.parse_args(argv)
    ET.register_namespace("user", "http://www.tableausoftware.com/xml/user")
    tree = ET.parse(a.twb)
    normalize(tree.getroot(), a.version)
    tree.write(a.out, encoding="utf-8", xml_declaration=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
