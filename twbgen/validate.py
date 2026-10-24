"""Validate a .twb against the official Tableau TWB XSD (tableau/tableau-document-schemas).

The published XSD imports the `xml:` and Tableau `user:` namespaces without a
schemaLocation, so xmllint cannot compile it as shipped. `xsd/*.local.xsd`
points those imports at two small stubs (`xml-stub.xsd`, `user-stub.xsd`).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

XSD_DIR = Path(__file__).resolve().parent / "xsd"
DEFAULT_XSD = XSD_DIR / "twb_2026.2.0.local.xsd"


def validate(twb: str | Path, xsd: str | Path = DEFAULT_XSD) -> tuple[bool, list[str]]:
    if shutil.which("xmllint") is None:
        raise RuntimeError("xmllint not found (macOS ships it; on Linux install libxml2-utils)")
    proc = subprocess.run(
        ["xmllint", "--noout", "--schema", str(xsd), str(twb)],
        capture_output=True, text=True,
    )
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    issues = [ln for ln in lines if not ln.endswith(" validates") and not ln.endswith(" fails to validate")]
    return proc.returncode == 0, issues


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("twb", nargs="+")
    p.add_argument("--xsd", default=str(DEFAULT_XSD))
    a = p.parse_args(argv)
    failed = 0
    for path in a.twb:
        ok, issues = validate(path, a.xsd)
        print(f"{'VALID  ' if ok else 'INVALID'} {path}")
        for issue in issues[:20]:
            print("   ", issue)
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
