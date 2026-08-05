"""Publish the training bundle as a GitHub release asset.

One-way and public, so it re-runs the safety checks itself rather than
trusting that they were done earlier in a terminal:

  1. the held-out evaluation set (releases 14-17) must not appear — that set
     is the only clean benchmark that exists and publishing it destroys it
     permanently, with no way to regenerate;
  2. no raw customer identifiers, account names or emails — hashed ids only;
  3. the manifest must be present and carry its caveat, so a miner reading
     only the file still learns what era it is from.

Refuses to publish if any check fails, and refuses to overwrite an existing
release asset (a silent replacement is how two miners end up training on
different files that share a name).
"""
import json
import os
import re
import sys

import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

REPO = "ippcteam/SN21-adtao"
TAG = "training-bundle-2026-08"
ASSET = "SN21_training_bundle.jsonl"
BUNDLE = os.path.expanduser("~/Downloads/SN21_training_bundle.jsonl")
HELD_OUT_RELEASES = (14, 15, 16, 17)

LEAK_KEYS = re.compile(
    r"customer_id$|account_name|campaign_name|customer_name|email|"
    r"descriptive_name|user_email|login", re.I)


def _api(path, method="GET", data=None, token=None, host="api.github.com"):
    req = urllib.request.Request(
        f"https://{host}/{path}", method=method,
        data=(json.dumps(data).encode() if data is not None else None),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 # GitHub REJECTS requests with urllib's default User-Agent
                 # and returns 403 — which reads exactly like a permissions
                 # failure and is not one. curl works on the same call with
                 # the same token; that discrepancy is the tell.
                 "User-Agent": "sn21-publish-script"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def safety_checks():
    """Everything that must hold before a byte leaves this machine."""
    print("safety checks")
    if not os.path.exists(BUNDLE):
        print(f"  FAIL: {BUNDLE} not found"); return None

    manifest, ids, leaks, raw_ids = None, set(), set(), 0
    with open(BUNDLE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_manifest" in rec:
                manifest = rec["_manifest"]
                continue
            ids.add(int(rec["episode_id"]))

            def walk(o):
                nonlocal raw_ids
                if isinstance(o, dict):
                    for k, v in o.items():
                        if LEAK_KEYS.search(k):
                            leaks.add(k)
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
                elif isinstance(o, str) and re.fullmatch(r"\d{3}-?\d{3}-?\d{4}", o):
                    raw_ids += 1
            walk(rec.get("input", {}))

    ok = True
    print(f"  records            : {len(ids)} ({len(ids)} unique)")

    if manifest is None:
        print("  FAIL: no manifest line"); ok = False
    elif not manifest.get("caveat"):
        print("  FAIL: manifest carries no caveat"); ok = False
    else:
        print("  manifest + caveat  : present")

    if leaks:
        print(f"  FAIL: identifier-like keys present: {sorted(leaks)}"); ok = False
    else:
        print("  identifier keys    : none (hashed ids only)")
    if raw_ids:
        print(f"  FAIL: {raw_ids} raw customer-id shaped strings"); ok = False
    else:
        print("  raw customer ids   : none")

    # held-out set — the check that matters most
    try:
        sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")
        from sqlalchemy import text as T

        from app.models import get_session
        with get_session() as s:
            held = {r[0] for r in s.execute(T(
                "SELECT id FROM bittensor_episode_candidates "
                "WHERE reserved_by_release_id IN :h"
            ).bindparams(__import__("sqlalchemy").bindparam("h", expanding=True)),
                {"h": list(HELD_OUT_RELEASES)}).fetchall()}
        leaked = ids & held
        if leaked:
            print(f"  FAIL: {len(leaked)} HELD-OUT episodes in the bundle — "
                  f"publishing would destroy the only clean benchmark")
            ok = False
        else:
            print(f"  held-out leakage   : 0 of {len(held)} — benchmark intact")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL: could not verify held-out exclusion ({type(e).__name__}: {e})")
        print("         refusing to publish without this check")
        ok = False

    return (manifest, len(ids)) if ok else None


def main():
    # Prefer the gh CLI's credentials over the .env token. The fine-grained
    # GH_TOKEN in .env has repo READ but not Contents:write, so it 403s on
    # release creation — reaching for it first meant an interactive login
    # succeeded and the publish still failed for the wrong reason.
    token = ""
    try:
        import subprocess
        # STRIP GH_TOKEN/GITHUB_TOKEN from the subprocess environment first.
        # load_dotenv() above puts the .env token into os.environ, and
        # `gh auth token` RETURNS the env token in preference to its keyring —
        # so asking gh for credentials would hand back the very weak token we
        # are trying to avoid, and the 403 looks like a keyring problem. It is
        # not; it is us poisoning gh's own lookup.
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        got = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=15, env=clean)
        if got.returncode == 0:
            token = got.stdout.strip()
            print("auth: gh CLI credentials")
    except Exception:  # noqa: BLE001
        pass
    if not token:
        token = (os.environ.get("GH_TOKEN")
                 or os.environ.get("GITHUB_TOKEN") or "").strip()
        if token:
            print("auth: .env token (needs Contents:write to create releases)")
    if not token:
        print("no GitHub credentials — run `gh auth login`", file=sys.stderr)
        return 2

    checked = safety_checks()
    if checked is None:
        print("\nREFUSING TO PUBLISH — a safety check failed", file=sys.stderr)
        return 1
    manifest, count = checked

    # existing release?
    try:
        rel = _api(f"repos/{REPO}/releases/tags/{TAG}", token=token)
        print(f"\nrelease {TAG} exists (id {rel['id']})")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        body = (
            f"Training bundle for the SN21 daily stream — **{count} settled "
            f"episodes**, one JSON object per line.\n\n"
            f"Each record is `{{episode_id, input, labels}}`: the input payload a "
            f"miner sees at prediction time, and the settled outcomes at 7, 14 "
            f"and 28 days (cost, conversions, CPA and conversion-value deltas).\n\n"
            f"**What this is drawn from.** {manifest.get('caveat', '')}\n\n"
            f"**Not scored.** {manifest.get('unscored_fields', '')}\n\n"
            f"A held-out evaluation set is retained and never published, so a "
            f"submitted model can be assessed on data it cannot have trained on.\n\n"
            f"How to use it: [docs/SN21_TRAINING.md]"
            f"(https://github.com/{REPO}/blob/main/docs/SN21_TRAINING.md)"
        )
        rel = _api(f"repos/{REPO}/releases", "POST", {
            "tag_name": TAG, "name": "SN21 training bundle",
            "body": body, "draft": False, "prerelease": False,
        }, token=token)
        print(f"\ncreated release {TAG} (id {rel['id']})")

    if any(a["name"] == ASSET for a in rel.get("assets", [])):
        print(f"asset {ASSET} already attached — refusing to overwrite "
              f"(delete it first if a replacement is intended)", file=sys.stderr)
        return 1

    size = os.path.getsize(BUNDLE)
    print(f"uploading {ASSET} ({size / 1e6:.1f} MB)...")
    upload = rel["upload_url"].split("{")[0] + f"?name={ASSET}"
    with open(BUNDLE, "rb") as f:
        req = urllib.request.Request(
            upload, data=f.read(), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req) as r:
            asset = json.load(r)

    print(f"\nPUBLISHED: {asset['browser_download_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
