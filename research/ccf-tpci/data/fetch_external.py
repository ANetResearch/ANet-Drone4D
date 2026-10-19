"""Fetch and verify the paper data files kept out of the repository: `python research/ccf-tpci/data/fetch_external.py [--check]`.

Files larger than the 5,000,000-byte release limit (check-release REL-01, ADR-079) are not committed. `external.json` next
to this script lists each of them with its byte count, SHA-256, the experiment command that regenerates it and, once
published, its download URL on the ModelScope dataset channel (ADR-201). The default action downloads every listed file
that is missing and verifies it; `--check` only verifies (exit 1 when a file is missing or differs).

Download source, in order: `--base-url`, the environment variable `AWR_PAPER_DATA_URL` (base URL; the file name is
appended), the `url` of the entry. Without any of them the script prints the regeneration command instead. A regenerated
file is a new measurement (its `_wall_s` column and row order differ), so its SHA-256 does not match the published one;
`--check` reports it as differing and the figure scripts accept it.
Standard library only (runs under any Python 3.10+).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "external.json"
SCHEMA = "awr.paper.external_data.v1"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(manifest: Path = MANIFEST) -> dict:
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise SystemExit(f"{manifest}: schema {doc.get('schema')!r}, expected {SCHEMA!r}")
    return doc


def status(entry: dict, root: Path = HERE) -> str:
    """ok | missing | differs"""
    p = root / entry["name"]
    if not p.is_file():
        return "missing"
    if p.stat().st_size != entry["bytes"] or sha256_file(p) != entry["sha256"]:
        return "differs"
    return "ok"


def source_url(entry: dict, base_url: str | None) -> str | None:
    base = base_url or os.environ.get("AWR_PAPER_DATA_URL")
    if base:
        return base.rstrip("/") + "/" + entry["name"]
    return entry.get("url")


def fetch(entry: dict, url: str, root: Path = HERE) -> None:
    """Download to a temporary file in the same directory, verify byte count and SHA-256, then rename into place."""
    fd, tmp = tempfile.mkstemp(prefix=f".{entry['name']}.", dir=root)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as r:  # https (ModelScope) or file URL
            while chunk := r.read(1 << 20):
                out.write(chunk)
        got = Path(tmp)
        if got.stat().st_size != entry["bytes"] or sha256_file(got) != entry["sha256"]:
            raise SystemExit(f"{entry['name']}: downloaded file from {url} does not match external.json (bytes or sha256)")
        os.replace(tmp, root / entry["name"])
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only; exit 1 when a file is missing or differs")
    ap.add_argument("--base-url", help="download base URL (overrides AWR_PAPER_DATA_URL and the per-file url)")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    a = ap.parse_args(argv)
    doc = load(a.manifest)
    root = a.manifest.resolve().parent
    rc = 0
    for e in doc["files"]:
        st = status(e, root)
        if st == "ok":
            print(f"{e['name']}: ok")
            continue
        if st == "differs":                     # never overwrite a local file: it may be a regenerated measurement
            print(f"{e['name']}: differs from external.json (locally regenerated or modified; not the published bytes)")
            rc = 1
            continue
        if a.check:
            print(f"{e['name']}: missing")
            rc = 1
            continue
        url = source_url(e, a.base_url)
        if not url:
            print(f"{e['name']}: missing; no download URL yet. Regenerate it with: {e['regenerate']}")
            rc = 2
            continue
        fetch(e, url, root)
        print(f"{e['name']}: fetched from {url}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
