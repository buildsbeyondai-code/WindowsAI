#!/usr/bin/env python3
"""Submit the newest GitHub release to the Microsoft Store, unattended.

Run by .github/workflows/store-submit.yml every hour (and after each release),
so a Store update no longer needs the owner's laptop to be on. Each run decides
one of:

* the newest release is already the published Store package -> nothing to do;
* another submission is in progress (for example a listing update still in
  certification) -> wait, try again next hour;
* a draft this tool created earlier was left uncommitted (a run that died
  half-way) -> commit it;
* otherwise -> download the release's x64 + arm64 MSIX, check them against
  SHA256SUMS, bundle them with makeappx, create a submission (Partner Center
  clones the last published one, so the listing, keywords and screenshots are
  kept untouched), replace the packages, upload, commit and wait until the
  commit is accepted.

Only the packages change. The listing is never written, so an SEO update
certified in Partner Center is never reverted.

Secrets: STORE_CLIENT_SECRET (required). STORE_TENANT_ID and STORE_CLIENT_ID
default to the SysPerf Entra app. GITHUB_TOKEN / GITHUB_REPOSITORY come from
Actions. ``--dry-run`` reports the decision without changing anything.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

APP_ID = "9P558WJTTC0B"
TENANT = os.environ.get("STORE_TENANT_ID") or "9362e228-a247-4d0f-888f-2fb96bdcffe4"
CLIENT = os.environ.get("STORE_CLIENT_ID") or "b8b889ca-c9d6-4a15-accf-1120352542a3"
API = "https://manage.devcenter.microsoft.com/v1.0/my/applications/" + APP_ID
ARCHES = ("x64", "arm64")


def log(msg: str) -> None:
    print(msg, flush=True)


def summary(msg: str) -> None:
    """A line in the run's summary page (and the log)."""
    log(msg)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ------------------------------------------------------------------ HTTP ----
def http(method: str, url: str, headers: dict | None = None, data: bytes | None = None,
         timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:800].decode(errors="replace")
        raise SystemExit(f"{method} {url.split('?')[0]} -> {e.code}: {body}") from None


def store_token(secret: str) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": CLIENT, "client_secret": secret,
        "resource": "https://manage.devcenter.microsoft.com"}).encode()
    raw = http("POST", f"https://login.microsoftonline.com/{TENANT}/oauth2/token", data=body)
    return json.loads(raw)["access_token"]


class Store:
    def __init__(self, token: str):
        self.h = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def call(self, method: str, path: str = "", data=None):
        raw = http(method, API + path, self.h,
                   None if data is None else json.dumps(data).encode())
        return json.loads(raw) if raw else {}


# -------------------------------------------------------------- release ----
def latest_release(repo: str, gh_token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if gh_token:
        h["Authorization"] = "Bearer " + gh_token
    return json.loads(http("GET", f"https://api.github.com/repos/{repo}/releases/latest", h))


def package_version(tag: str) -> str:
    """'v5.5.3' -> '5.5.3.0' (the MSIX four-part version)."""
    parts = tag.lstrip("v").split(".")
    return ".".join([*parts, "0", "0", "0", "0"][:4])


def has_version(submission: dict, version: str) -> bool:
    for p in submission.get("applicationPackages") or []:
        if p.get("fileStatus") == "PendingDelete":
            continue
        if p.get("version") == version or f"_{version}" in (p.get("fileName") or ""):
            return True
    return False


def bundle_name(version: str) -> str:
    return f"SysPerf_{version}.msixbundle"


def makeappx() -> str:
    found = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\10.*\x64\makeappx.exe"))
    if not found:
        raise SystemExit("makeappx.exe not found (Windows SDK missing on this runner)")
    return found[-1]


def build_upload(release: dict, version: str, work: str) -> str:
    """Download, verify, bundle and zip. Returns the path of upload.zip."""
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    names = [f"SysPerf_{version}_{a}.msix" for a in ARCHES]
    missing = [n for n in ["SHA256SUMS.txt", *names] if n not in assets]
    if missing:
        raise SystemExit(f"release {release['tag_name']} lacks {missing}")
    sums = {}
    for line in http("GET", assets["SHA256SUMS.txt"], timeout=300).decode().splitlines():
        if line.strip():
            h, n = line.split(None, 1)
            sums[n.strip().lstrip("*")] = h.lower()
    bundle_in = os.path.join(work, "bundle-in")
    os.makedirs(bundle_in)
    for n in names:
        data = http("GET", assets[n], timeout=1800)
        digest = hashlib.sha256(data).hexdigest()
        if sums.get(n) != digest:
            raise SystemExit(f"SHA-256 mismatch for {n}: {digest} != {sums.get(n)}")
        with open(os.path.join(bundle_in, n), "wb") as f:
            f.write(data)
        log(f"verified {n} ({len(data) / 1e6:.1f} MB)")
    bundle = os.path.join(work, bundle_name(version))
    subprocess.run([makeappx(), "bundle", "/o", "/d", bundle_in, "/p", bundle,
                    "/bv", version], check=True)
    upload = os.path.join(work, "upload.zip")
    with zipfile.ZipFile(upload, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(bundle, bundle_name(version))
    return upload


# ----------------------------------------------------------- submission ----
def commit_and_wait(store: Store, sid: str, minutes: int = 20) -> None:
    store.call("POST", f"/submissions/{sid}/commit")
    deadline = time.time() + minutes * 60
    while True:
        st = store.call("GET", f"/submissions/{sid}/status")
        status = st.get("status")
        log(f"{time.strftime('%H:%M:%S')} submission {sid}: {status}")
        if status == "CommitFailed":
            raise SystemExit("commit failed: " + json.dumps(st.get("statusDetails"))[:1500])
        if status != "CommitStarted":
            summary(f"Submission `{sid}` committed; status **{status}**.")
            return
        if time.time() > deadline:
            summary(f"Submission `{sid}` still processing after {minutes} min; "
                    "Partner Center will finish it.")
            return
        time.sleep(30)


def run(dry_run: bool) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "buildsbeyondai-code/WindowsAI")
    release = latest_release(repo, os.environ.get("GITHUB_TOKEN"))
    version = package_version(release["tag_name"])
    log(f"newest GitHub release: {release['tag_name']} -> package {version}")

    secret = os.environ.get("STORE_CLIENT_SECRET")
    if not secret:
        summary("STORE_CLIENT_SECRET is not set: add it under Settings > Secrets and "
                "variables > Actions. Nothing submitted.")
        return 0
    store = Store(store_token(secret))
    app = store.call("GET")

    published = (app.get("lastPublishedApplicationSubmission") or {}).get("id")
    if published and has_version(store.call("GET", f"/submissions/{published}"), version):
        summary(f"{version} is already the published Store package. Nothing to do.")
        return 0

    pending = (app.get("pendingApplicationSubmission") or {}).get("id")
    if pending:
        sub = store.call("GET", f"/submissions/{pending}")
        status = sub.get("status")
        ours = has_version(sub, version)
        if ours and status not in ("PendingCommit", "CommitFailed"):
            # In flight (pre-processing, certification, publication), or failed
            # certification, which needs a person in Partner Center, not a retry.
            summary(f"{version} is already submitted (`{pending}`, {status}).")
            return 0
        if ours and status == "PendingCommit":
            pkg = [p for p in sub["applicationPackages"]
                   if p.get("fileName") == bundle_name(version)]
            if pkg and pkg[0].get("fileStatus") == "Uploaded":
                summary(f"Committing the uploaded draft `{pending}` left by an earlier run.")
                if not dry_run:
                    commit_and_wait(store, pending)
                return 0
        summary(f"Waiting: submission `{pending}` is **{status}**. "
                f"{version} will be submitted once it is published.")
        return 0

    if dry_run:
        summary(f"Dry run: would submit {version} now.")
        return 0

    work = tempfile.mkdtemp(prefix="store-")
    try:
        upload = build_upload(release, version, work)
        sub = store.call("POST", "/submissions")          # clone of the published one
        sid = sub["id"]
        for p in sub.get("applicationPackages") or []:
            p["fileStatus"] = "PendingDelete"
        sub["applicationPackages"].append({
            "fileName": bundle_name(version), "fileStatus": "PendingUpload",
            "minimumDirectXVersion": "None", "minimumSystemRam": "None"})
        # Packages only: listings, pricing and properties go back unchanged.
        store.call("PUT", f"/submissions/{sid}", sub)
        with open(upload, "rb") as f:
            http("PUT", sub["fileUploadUrl"], {"x-ms-blob-type": "BlockBlob"}, f.read(),
                 timeout=3600)
        log(f"uploaded {bundle_name(version)} to submission {sid}")
        commit_and_wait(store, sid)
        summary(f"Submitted **{version}** to the Microsoft Store (`{sid}`).")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report the decision only")
    return run(ap.parse_args(argv).dry_run)


if __name__ == "__main__":
    sys.exit(main())
