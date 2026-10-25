"""The public release list — docs/SN21_MODEL_PRIVACY.md ("Each release is
written to a public release ledger").

After the day's release step, every released digest is published to the
operator's public repository: released.json (digest, hotkey, released_on,
reason, closed_since and the operator's copy) and a README with the same
table. Only releases are listed; a digest that is still closed is not.

Switches (both needed; either unset = nothing is published):
  SN21_RELEASE_LIST_REPO     owner/name, for example adtaoai/sn21_models
  SN21_RELEASE_LIST_TOKEN    a token that may write that repository's contents

A file is written only when its content changes. Never raises: the caller
logs the summary, and a failure here must not stop the run.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

REPO_ENV = "SN21_RELEASE_LIST_REPO"
TOKEN_ENV = "SN21_RELEASE_LIST_TOKEN"
LIST_FILE = "released.json"
README_FILE = "README.md"
FIELDS = ("digest", "hotkey", "released_on", "reason", "closed_since")


def public_rows(ledger: dict, mirrored: dict) -> list[dict]:
    """One row per release, with only the public fields and the copy, if made."""
    rows = []
    for r in ledger.get("releases", []):
        row = {k: r.get(k) for k in FIELDS}
        copy = (mirrored.get(r.get("digest")) or {}).get("target")
        row["copy"] = copy
        rows.append(row)
    return sorted(rows, key=lambda x: (x["released_on"] or "", x["digest"] or ""))


def list_json(ledger: dict, rows: list[dict]) -> str:
    doc = {"rule": "https://github.com/ippcteam/SN21-adtao/blob/main/docs/SN21_MODEL_PRIVACY.md",
           "from": ledger.get("from"), "as_of": ledger.get("as_of"), "released": rows}
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def readme(ledger: dict, rows: list[dict]) -> str:
    lines = [
        "# SN21 released models",
        "",
        ("Models released under the SN21 model release rule "
         "([SN21_MODEL_PRIVACY.md](https://github.com/ippcteam/SN21-adtao/blob/main/docs/SN21_MODEL_PRIVACY.md))."),
        "A released digest stays released. `copy` is the operator's public copy of the same digest.",
        "",
        (f"Rule observed from {ledger.get('from')}; list as of {ledger.get('as_of')}. "
         f"Machine-readable: [{LIST_FILE}]({LIST_FILE})."),
        "",
        "| Released on | Reason | Hotkey | Digest | Copy |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for r in rows:
        lines.append(f"| {r['released_on']} | {r['reason']} | `{r['hotkey']}` | `{r['digest']}` | "
                     + (f"`{r['copy']}`" if r["copy"] else "pending") + " |")
    if not rows:
        lines.append("| (none yet) | | | | |")
    return "\n".join(lines) + "\n"


def _api(path: str, token: str, method: str = "GET", data: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com/{path}", method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 # GitHub refuses urllib's default User-Agent with a 403
                 "User-Agent": "sn21-release-list"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _put_if_changed(repo: str, path: str, text: str, token: str, message: str, api=_api) -> str:
    sha = None
    try:
        cur = api(f"repos/{repo}/contents/{path}", token)
        sha = cur.get("sha")
        if base64.b64decode(cur.get("content", "")).decode() == text:
            return "same"
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    body = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    api(f"repos/{repo}/contents/{path}", token, "PUT", body)
    return "written"


def publish_release_list(ledger: dict, mirrored: dict, environ=os.environ, api=_api) -> dict:
    repo = (environ.get(REPO_ENV) or "").strip()
    token = (environ.get(TOKEN_ENV) or "").strip()
    if not repo or not token:
        return {"skipped": f"{REPO_ENV} or {TOKEN_ENV} not set"}
    rows = public_rows(ledger, mirrored)
    message = f"release list as of {ledger.get('as_of')}"
    out = {"released": len(rows)}
    for path, text in ((LIST_FILE, list_json(ledger, rows)), (README_FILE, readme(ledger, rows))):
        try:
            out[path] = _put_if_changed(repo, path, text, token, message, api)
        except Exception as exc:                       # written again tomorrow
            out[path] = f"error: {str(exc)[:200]}"
    return out
