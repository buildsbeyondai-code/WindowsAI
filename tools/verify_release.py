#!/usr/bin/env python3
"""Check a SysPerf release's files against its SHA256SUMS.txt.

Use it before installing a downloaded .msix by hand, before re-submitting an
older release to the Store during a rollback (docs/RECOVERY.md), or to confirm
that a GitHub Release still holds exactly what CI built.

    python tools/verify_release.py DIR          # files already downloaded
    python tools/verify_release.py --tag v5.5.3 # download with the gh CLI first

Every file named in SHA256SUMS.txt must be present and match. Any other
package file (.msix, .msixbundle, .appx) in the folder fails the check too:
an unlisted package is exactly what tampering would look like. Exit code 0
means verified, 1 means a mismatch or a missing or unlisted file, 2 means a
usage or download error. Nothing is changed or uploaded.

The sums are written by the same workflow that publishes the files, so this
proves the files are the ones CI published and were not corrupted or swapped
afterwards. It does not prove who ran CI; build attestations would add that.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SUMS = "SHA256SUMS.txt"
PACKAGE_SUFFIXES = (".msix", ".msixbundle", ".appx", ".appxbundle")
_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output -> {file name: lower-case hex digest}."""
    sums: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            raise ValueError(f"{SUMS} line {n} is not '<sha256>  <file>': {line[:80]}")
        name = m.group(2).strip()
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"{SUMS} line {n} names a path, not a file: {name}")
        if name in sums:
            raise ValueError(f"{SUMS} lists {name} twice")
        sums[name] = m.group(1).lower()
    if not sums:
        raise ValueError(f"{SUMS} is empty")
    return sums


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(folder: Path) -> list[str]:
    """Problems found in ``folder``; an empty list means every file verified."""
    sums_file = folder / SUMS
    if not sums_file.is_file():
        return [f"{SUMS} not found in {folder}"]
    try:
        sums = parse_sums(sums_file.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        return [str(exc)]
    problems = []
    for name, want in sorted(sums.items()):
        path = folder / name
        if not path.is_file():
            problems.append(f"missing: {name}")
        elif sha256(path) != want:
            problems.append(f"checksum mismatch: {name}")
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in PACKAGE_SUFFIXES and path.name not in sums:
            problems.append(f"not listed in {SUMS}: {path.name}")
    return problems


def download(tag: str, repo: str, dest: Path) -> None:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("the gh CLI is needed for --tag (https://cli.github.com)")
    r = subprocess.run([gh, "release", "download", tag, "--repo", repo, "--dir", str(dest)],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh release download {tag} failed: {r.stderr.strip()[:300]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("folder", nargs="?", type=Path, help="folder holding the release files")
    src.add_argument("--tag", help="download this release with gh first, e.g. v5.5.3")
    ap.add_argument("--repo", default="buildsbeyondai-code/WindowsAI",
                    help="owner/name for --tag (default: %(default)s)")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="sysperf-verify-") as tmp:
        folder = args.folder
        if args.tag:
            folder = Path(tmp)
            try:
                download(args.tag, args.repo, folder)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        if not folder.is_dir():
            print(f"error: {folder} is not a folder", file=sys.stderr)
            return 2
        problems = verify(folder)
        label = args.tag or str(folder)
        if problems:
            print(f"FAILED {label}:")
            for p in problems:
                print(f"  - {p}")
            return 1
        names = sorted(parse_sums((folder / SUMS).read_text(encoding="utf-8-sig")))
        print(f"verified {label}: {len(names)} file(s) match {SUMS}")
        for n in names:
            print(f"  ok {n}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
