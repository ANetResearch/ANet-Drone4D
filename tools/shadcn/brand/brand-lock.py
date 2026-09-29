#!/usr/bin/env python3
"""apps/web/public/brand/brand.lock.json: sha256, source and date of every brand asset (M15-FR-109; AWR-15 §4.1, §4.8).

check-brand (BRAND-01) compares every file in public/brand with this lock and anet-logo.svg with the reference repository.
Existing dates are kept when the bytes did not change, so the lock is stable across reruns.
Usage: python3 tools/shadcn/brand/brand-lock.py
"""

import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BRAND = ROOT / "apps/web/public/brand"
AVATAR = "https://avatars.githubusercontent.com/u/305781773"
SOURCES = {
    "anet-logo.svg": "refs/design/ANet/docs/media/anet-logo.svg",
    "avatar-96.png": f"{AVATAR}?s=96&v=4",
    "avatar-460.png": f"{AVATAR}?s=460&v=4",
    "favicon-32.png": "avatar-96.png, 3x3 premultiplied box filter (tools/shadcn/brand/make-favicons.py)",
    "favicon-16.png": "avatar-96.png, 6x6 premultiplied box filter (tools/shadcn/brand/make-favicons.py)",
}


def main():
    lock_path = BRAND / "brand.lock.json"
    old = {}
    if lock_path.exists():
        old = {e["name"]: e for e in json.loads(lock_path.read_text(encoding="utf-8"))["files"]}
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    files = []
    for name, source in SOURCES.items():
        digest = hashlib.sha256((BRAND / name).read_bytes()).hexdigest()
        prev = old.get(name)
        date = prev["date"] if prev and prev.get("sha256") == digest else today
        files.append(
            {"name": name, "sha256": digest, "bytes": (BRAND / name).stat().st_size, "source": source, "date": date}
        )
    doc = {
        "$comment": "Brand asset lock (ADR-032; AWR-15 §4.1). Regenerate with tools/shadcn/brand/fetch-brand.sh or brand-lock.py.",
        "files": files,
    }
    lock_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"brand-lock: {len(files)} files")


if __name__ == "__main__":
    main()
