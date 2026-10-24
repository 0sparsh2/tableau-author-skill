"""Extract a Qt binary resource file (.rcc) to a directory, read-only on the input.

Supports rcc format versions 1-3 with zlib (qCompress) and zstd payloads
(zstd needs Python 3.14+ `compression.zstd`). Usage:
    python3 -I extract_qt_rcc.py IN.rcc OUT_DIR [--list]
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

try:
    from compression import zstd  # Python 3.14+
except ImportError:  # pragma: no cover
    zstd = None

COMPRESSED, DIRECTORY, ZSTD = 0x01, 0x02, 0x04


def main(argv: list[str]) -> int:
    src, out_dir = Path(argv[0]), Path(argv[1])
    list_only = "--list" in argv
    buf = src.read_bytes()
    if buf[:4] != b"qres":
        raise SystemExit("not a Qt rcc file")
    version, tree_off, data_off, names_off = struct.unpack(">IIII", buf[4:20])
    node_size = 14 if version == 1 else 22

    def name_at(off: int) -> str:
        p = names_off + off
        (length,) = struct.unpack(">H", buf[p:p + 2])
        return buf[p + 6:p + 6 + 2 * length].decode("utf-16-be")

    def node(i: int):
        p = tree_off + i * node_size
        name_off, flags = struct.unpack(">IH", buf[p:p + 6])
        if flags & DIRECTORY:
            count, first = struct.unpack(">II", buf[p + 6:p + 14])
            return name_off, flags, count, first
        _country, _lang, data = struct.unpack(">HHI", buf[p + 6:p + 14])
        return name_off, flags, None, data

    out_root = out_dir.resolve()
    files = total = 0

    def walk(i: int, rel: Path) -> None:
        nonlocal files, total
        name_off, flags, count, ref = node(i)
        name = name_at(name_off) if i else ""
        if name in ("..", ".") or "/" in name:
            raise SystemExit(f"unsafe name {name!r}")
        path = rel / name if name else rel
        if flags & DIRECTORY:
            for c in range(ref, ref + count):
                walk(c, path)
            return
        p = data_off + ref
        (size,) = struct.unpack(">I", buf[p:p + 4])
        blob = buf[p + 4:p + 4 + size]
        if flags & COMPRESSED:
            blob = zlib.decompress(blob[4:])  # qCompress: 4-byte BE length prefix
        elif flags & ZSTD:
            if zstd is None:
                raise SystemExit("zstd payload needs Python 3.14+")
            blob = zstd.decompress(blob)
        files += 1
        total += len(blob)
        if list_only:
            print(f"{len(blob):>10} {path}")
            return
        dest = (out_root / path).resolve()
        if out_root not in dest.parents:
            raise SystemExit(f"path escapes output dir: {path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)

    walk(0, Path("."))
    print(f"rcc v{version}: {files} files, {total / 1e6:.1f} MB uncompressed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
