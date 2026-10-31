#!/usr/bin/env python3
"""One-off: attach the setup exe to v1.2.0 and refresh SHA256SUMS.txt."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "lingyi-ling/novel-downloader"
TAG = "v1.2.0"
SETUP = ROOT / "dist" / "NovelDownloader-setup-1.2.0-x64.exe"


def gh_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError("no github credential found")


def api(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    token = gh_token()
    release = api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}", token)
    upload_url = release["upload_url"].split("{")[0]

    # 1. upload the setup exe
    req = urllib.request.Request(
        f"{upload_url}?name={SETUP.name}",
        data=SETUP.read_bytes(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-msdownload"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read().decode())
        print(f"uploaded {result['name']} ({result['size'] / 1e6:.1f} MB)")

    # 2. rebuild SHA256SUMS.txt from all current assets
    sums = {}
    for asset in release["assets"]:
        if asset["name"] == "SHA256SUMS.txt":
            continue
        with urllib.request.urlopen(urllib.request.Request(  # noqa: S310
            asset["browser_download_url"],
            headers={"Authorization": f"Bearer {token}"},
        ), timeout=600) as resp:
            digest = hashlib.sha256(resp.read()).hexdigest()
        sums[asset["name"]] = digest
    sums[SETUP.name] = hashlib.sha256(SETUP.read_bytes()).hexdigest()
    body = "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items()))
    (ROOT / "dist" / "SHA256SUMS.txt").write_text(body, encoding="utf-8")

    # 3. delete + re-upload SHA256SUMS.txt
    for asset in release["assets"]:
        if asset["name"] == "SHA256SUMS.txt":
            api("DELETE",
                f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}",
                token)
    req = urllib.request.Request(
        f"{upload_url}?name=SHA256SUMS.txt",
        data=body.encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"uploaded {json.loads(resp.read().decode())['name']}")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
