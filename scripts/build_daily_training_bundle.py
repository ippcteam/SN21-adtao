"""Build the daily-stream training bundle — real daily episodes + their SETTLED
outcomes, in the shape miners are scored on (7/14/28-day horizons).

    python3 -m scripts.build_daily_training_bundle --from 2026-08-06 --to 2026-08-31
    # writes SN21_daily_training_bundle.jsonl + prints coverage stats
    # add --out PATH to choose the file; publishing is a separate, deliberate step.

WHY THIS EXISTS
    The published training bundle is weekly-era (WR-* epochs, 7/14-day). The live
    contract is the daily stream (BD-* baskets, 7/14/**28**-day). A model trains
    best on data shaped like what it is scored on. This builds that: each record
    is {episode_id, input, labels} where `input` is exactly what a miner sees at
    prediction time and `labels` are the outcomes that have SETTLED so far.

LEAK-FREE BY CONSTRUCTION
    We include ONLY horizons whose outcome has actually settled (is served by the
    operator's outcomes endpoint). A basket's 14/28-day labels simply do not
    appear until they mature, so a future label can never leak into training. The
    validator-only field is dropped from `input`. Re-run as outcomes mature and
    the bundle grows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest.daily_training_bundle import (  # noqa: E402
    build_records,
    validate_leakfree,
)


REPO = "ippcteam/SN21-adtao"
DAILY_TAG = "training-bundle-daily"
DAILY_ASSET = "SN21_daily_training_bundle.jsonl"


def _api_get(path):
    base = (os.environ.get("HOPE_API_URL") or "").rstrip("/")
    key = (os.environ.get("HOPE_API_KEY") or "").strip()
    if not base or not key:
        raise SystemExit("HOPE_API_URL and HOPE_API_KEY are required")
    req = urllib.request.Request(
        f"{base}/internal/bittensor/v1/{path}", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def _gh_token():
    """gh CLI credentials first (the fine-grained .env token lacks Contents:
    write), matching scripts/_publish_training_bundle.py."""
    try:
        import subprocess
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        got = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=15, env=clean, check=False)
        if got.returncode == 0 and got.stdout.strip():
            return got.stdout.strip(), "gh CLI"
    except Exception:
        pass
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    return tok, ".env"


def _gh(path, token, method="GET", data=None, host="api.github.com"):
    url = f"https://{host}/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def publish_bundle(path, count, dry_run=True):
    """Publish/refresh the daily training bundle as a GitHub release asset.

    The daily bundle GROWS as outcomes settle, so unlike the frozen weekly asset
    this REPLACES the existing asset (delete + re-upload). Dry-run prints the
    plan and makes no GitHub calls."""
    token, src = _gh_token()
    if not token:
        print("no GitHub credentials — run `gh auth login`", file=sys.stderr)
        return 2
    if dry_run:
        print(f"[publish] DRY RUN — would refresh {DAILY_ASSET} ({count} records) "
              f"on release {DAILY_TAG} of {REPO} (auth: {src}); existing asset "
              f"would be replaced. No GitHub calls made.")
        return 0
    # find or create the release
    try:
        rel = _gh(f"repos/{REPO}/releases/tags/{DAILY_TAG}", token)
        print(f"[publish] release {DAILY_TAG} exists (id {rel['id']})")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        body = (f"Daily-stream training bundle — real BD-* episodes with their "
                f"SETTLED 7/14/28-day outcomes, one JSON object per line "
                f"(`{{episode_id, input, labels}}`). Refreshed as outcomes mature. "
                f"A held-out evaluation set is never published. "
                f"See docs/SN21_TRAINING.md.")
        rel = _gh(f"repos/{REPO}/releases", token, "POST", {
            "tag_name": DAILY_TAG, "name": "SN21 daily training bundle",
            "body": body, "draft": False, "prerelease": False})
        print(f"[publish] created release {DAILY_TAG} (id {rel['id']})")
    # replace the asset (the bundle grows, so overwrite is intended)
    for a in rel.get("assets", []):
        if a["name"] == DAILY_ASSET:
            _gh(f"repos/{REPO}/releases/assets/{a['id']}", token, "DELETE")
            print(f"[publish] removed previous asset (id {a['id']})")
    size = os.path.getsize(path)
    print(f"[publish] uploading {DAILY_ASSET} ({size / 1e6:.2f} MB)...")
    up = rel["upload_url"].split("{")[0] + f"?name={DAILY_ASSET}"
    with open(path, "rb") as f:
        req = urllib.request.Request(up, data=f.read(), method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=300) as r:
            asset = json.loads(r.read().decode())
    print(f"[publish] done -> {asset.get('browser_download_url')}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", dest="to", required=True)
    p.add_argument("--out", default="SN21_daily_training_bundle.jsonl")
    p.add_argument("--dry-run", action="store_true",
                   help="build + stats + leak-check, but write nothing "
                        "(with --publish: print the publish plan, no GitHub calls)")
    p.add_argument("--publish", action="store_true",
                   help="after writing, refresh the GitHub release asset "
                        "(replaces the growing bundle)")
    args = p.parse_args()

    d0 = date.fromisoformat(args.frm)
    d1 = date.fromisoformat(args.to)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]

    # 1) episodes (X) from the baskets
    episodes_by_id, per_day = {}, {}
    for d in days:
        s, pkg = _api_get(f"releases/BD-{d.isoformat()}/package")
        if s != 200 or not isinstance(pkg, dict):
            per_day[d.isoformat()] = 0
            continue
        n = 0
        for ep in pkg.get("episodes", []):
            eid = str(ep.get("episode_id"))
            if ep.get("episode_id"):
                episodes_by_id[eid] = ep
                n += 1
        per_day[d.isoformat()] = n

    # 2) settled outcomes (y) — everything settled as of today
    today = datetime.now(timezone.utc).date().isoformat()
    s, out = _api_get(f"daily/outcomes?settled_on_or_before={today}")
    outcomes_by_id = {}
    for r in (out.get("outcomes", []) if isinstance(out, dict) else []):
        eid = str(r.get("episode_id"))
        if eid in episodes_by_id:
            outcomes_by_id.setdefault(eid, []).append(r)

    records = build_records(episodes_by_id, outcomes_by_id)
    problems = validate_leakfree(records)

    # stats
    print(f"baskets {d0}..{d1}: {sum(per_day.values())} episodes")
    hcov = {"7": 0, "14": 0, "28": 0}
    for rec in records:
        for h in rec["labels"]:
            hcov[h] = hcov.get(h, 0) + 1
    print(f"labelled records: {len(records)}  (of {len(episodes_by_id)} episodes)")
    print(f"  by horizon — 7d: {hcov['7']}  14d: {hcov['14']}  28d: {hcov['28']}")
    print(f"leak check: {'CLEAN' if not problems else str(len(problems)) + ' PROBLEMS'}")
    for x in problems[:5]:
        print(f"  - {x}")
    if problems:
        return 1

    if args.dry_run:
        print("DRY RUN — nothing written.")
        if args.publish:
            return publish_bundle(args.out, len(records), dry_run=True)
        return 0
    if not records:
        print("No settled labels yet — nothing to write (re-run once outcomes mature).")
        return 0

    with open(args.out, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(records)} records -> {args.out} "
          f"({os.path.getsize(args.out) / 1e6:.2f} MB)")

    if args.publish:
        return publish_bundle(args.out, len(records), dry_run=False)
    print("Publishing to a release is a separate step (--publish).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
