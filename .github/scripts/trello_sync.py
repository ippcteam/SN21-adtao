#!/usr/bin/env python3
"""Keep a Trello board up to date from this repository's commits.

Three modes, all run by .github/workflows/trello.yml:

  push      every push to the default branch: a commit whose message names a card
            (trello:<card short id or URL>) adds a comment to that card with the commit and its
            link, adds the token owner to the card, and moves it to the "done" list.
  daily     one "shipped" card for one day: the day's commits on the default branch in plain
            titles with links, the token owner as its member. Run again for the same day, it
            updates the card instead of adding another. A day with no commits adds nothing.
  backfill  "daily" for each of the last N days (default 7).

Whose work it is follows the token: each repository's TRELLO_TOKEN belongs to the person the
work is credited to, so the board's activity and the cards' members show that person.

Settings (repository secrets): TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD (the board's short id from
its URL, trello.com/b/<short id>/...). Optional variables: TRELLO_DONE_LIST (default "Done"),
TRELLO_SHIPPED_LIST (default "Shipped"), TRELLO_DAY_TZ (default "Asia/Karachi"), TRELLO_REPO_LABEL
(default the repository name). Lists that do not exist are created. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

API = "https://api.trello.com/1"
CARD_REF = re.compile(r"trello[:#]\s*(?:https?://trello\.com/c/)?([A-Za-z0-9]{8,24})", re.I)
MAX_TITLES = 150


# ------------------------------------------------------------------ pure ----

def card_refs(message: str) -> List[str]:
    """Card short ids a commit message names, in order, without repeats."""
    return list(dict.fromkeys(CARD_REF.findall(message or "")))


def day_window(day: date, tz: str):
    """[start, end) of a calendar day in the given time zone, as aware datetimes."""
    z = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=z)
    return start, start + timedelta(days=1)


def shipped_card(repo_label: str, day: date, commits: List[Dict[str, str]], repo_url: str) -> Dict[str, str]:
    """Title and description of the day's shipped card."""
    n = len(commits)
    title = f"{repo_label}: shipped {day.isoformat()} ({n} commit{'s' if n != 1 else ''})"
    lines = [f"Commits to the default branch of {repo_label} on {day.strftime('%A %d %B %Y')}:", ""]
    for c in commits[:MAX_TITLES]:
        lines.append(f"- {c['subject']} ([{c['sha'][:9]}]({repo_url}/commit/{c['sha']}))")
    if n > MAX_TITLES:
        lines.append(f"- and {n - MAX_TITLES} more")
    return {"name": title, "desc": "\n".join(lines)[:16000]}


def comment_for(commit: Dict[str, str], repo_label: str, repo_url: str) -> str:
    return (f"Shipped in {repo_label}: {commit['subject']}\n"
            f"{repo_url}/commit/{commit['sha']} ({commit.get('author', '')}, {commit.get('when', '')})")


# ------------------------------------------------------------------ trello --

class Trello:
    def __init__(self, key: str, token: str, board: str):
        self.auth = {"key": key, "token": token}
        self.board = board
        self._lists: Optional[Dict[str, str]] = None
        self._me: Optional[str] = None

    def call(self, method: str, path: str, **params):
        q = urllib.parse.urlencode({**self.auth, **{k: v for k, v in params.items() if v is not None}})
        req = urllib.request.Request(f"{API}{path}?{q}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            # never print the request URL: it carries the key and token
            raise RuntimeError(f"Trello {method} {path} -> {e.code}: {e.read()[:200]!r}") from None

    def me(self) -> str:
        if self._me is None:
            self._me = self.call("GET", "/members/me", fields="id,username")["id"]
        return self._me

    def list_id(self, name: str) -> str:
        if self._lists is None:
            self._lists = {l["name"].strip().lower(): l["id"]
                           for l in self.call("GET", f"/boards/{self.board}/lists", filter="open")}
        lid = self._lists.get(name.strip().lower())
        if lid is None:
            board_id = self.call("GET", f"/boards/{self.board}", fields="id")["id"]
            lid = self.call("POST", "/lists", name=name, idBoard=board_id, pos="bottom")["id"]
            self._lists[name.strip().lower()] = lid
        return lid

    def upsert_card(self, list_name: str, name: str, desc: str) -> str:
        lid = self.list_id(list_name)
        for c in self.call("GET", f"/lists/{lid}/cards", fields="name"):
            if c["name"] == name:
                self.call("PUT", f"/cards/{c['id']}", desc=desc)
                return c["id"]
        return self.call("POST", "/cards", idList=lid, name=name, desc=desc, idMembers=self.me(), pos="top")["id"]

    def ship_to_card(self, short_id: str, comment: str, done_list: str) -> None:
        card = self.call("GET", f"/cards/{short_id}", fields="id,idMembers,idList")
        self.call("POST", f"/cards/{card['id']}/actions/comments", text=comment)
        if self.me() not in (card.get("idMembers") or []):
            self.call("POST", f"/cards/{card['id']}/idMembers", value=self.me())
        done = self.list_id(done_list)
        if card.get("idList") != done:
            self.call("PUT", f"/cards/{card['id']}", idList=done, pos="top")


# ------------------------------------------------------------------ git -----

def commits_between(start: datetime, end: datetime, ref: str, scan: int = 3000) -> List[Dict[str, str]]:
    """Commits on `ref` whose commit date (when they landed) falls in [start, end), oldest first.
    Filtered here rather than with git's --since/--until: those stop walking history at the first
    older commit, so a rebased or out-of-order history silently loses newer commits."""
    out = subprocess.run(["git", "log", ref, "--no-merges", f"-n{scan}",
                          "--format=%H%x1f%s%x1f%an%x1f%cI"],
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        sha, subject, author, when = line.split("\x1f")
        landed = datetime.fromisoformat(when)
        if start <= landed < end:
            rows.append({"sha": sha, "subject": subject, "author": author, "when": when, "_t": landed})
    rows.sort(key=lambda r: r["_t"])
    for r in rows:
        r.pop("_t")
    return rows


def pushed_commits() -> List[Dict[str, str]]:
    """Every commit the push brought, read from git (the event lists at most 20)."""
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    before, after = event.get("before") or "", event.get("after") or ""
    if before and set(before) != {"0"} and after:
        out = subprocess.run(["git", "log", f"{before}..{after}", "--no-merges", "--reverse",
                              "--format=%H%x1f%s%x1f%an%x1f%aI%x1f%B%x1e"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            rows = []
            for rec in out.stdout.split("\x1e"):
                rec = rec.strip("\n")
                if not rec:
                    continue
                sha, subject, author, when, message = rec.split("\x1f", 4)
                rows.append({"sha": sha, "subject": subject, "author": author, "when": when, "message": message})
            return rows
    return [{"sha": c["id"], "subject": c["message"].splitlines()[0], "message": c["message"],
             "author": (c.get("author") or {}).get("name", ""), "when": c.get("timestamp", "")}
            for c in event.get("commits") or []]


# ------------------------------------------------------------------ main ----

def main(argv: List[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "push"
    key, token, board = (os.environ.get(k, "").strip() for k in ("TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD"))
    if not (key and token and board):
        print("Trello sync skipped: TRELLO_KEY, TRELLO_TOKEN or TRELLO_BOARD is not set for this repository.")
        return 0
    t = Trello(key, token, board)
    repo = os.environ.get("GITHUB_REPOSITORY", "repo")
    label = os.environ.get("TRELLO_REPO_LABEL") or repo.split("/")[-1]
    repo_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
    tz = os.environ.get("TRELLO_DAY_TZ") or "Asia/Karachi"
    done_list = os.environ.get("TRELLO_DONE_LIST") or "Done"
    shipped_list = os.environ.get("TRELLO_SHIPPED_LIST") or "Shipped"
    ref = os.environ.get("TRELLO_REF") or "HEAD"

    if mode == "push":
        n = 0
        for c in pushed_commits():
            for short_id in card_refs(c["message"]):
                try:
                    t.ship_to_card(short_id, comment_for(c, label, repo_url), done_list)
                    n += 1
                    print(f"card {short_id}: commented, moved to {done_list}")
                except RuntimeError as e:
                    print(f"card {short_id}: not updated ({e})")
        print(f"{n} card update(s)")
        return 0

    if mode in ("daily", "backfill"):
        today = datetime.now(ZoneInfo(tz)).date()
        if mode == "daily":
            days = [date.fromisoformat(os.environ["TRELLO_DAY"]) if os.environ.get("TRELLO_DAY") else today]
        else:
            n = int(os.environ.get("TRELLO_DAYS") or 7)
            days = [today - timedelta(days=i) for i in range(n, -1, -1)]
        for d in days:
            start, end = day_window(d, tz)
            commits = commits_between(start, end, ref)
            if not commits:
                print(f"{d}: no commits, no card")
                continue
            card = shipped_card(label, d, commits, repo_url)
            t.upsert_card(shipped_list, card["name"], card["desc"])
            print(f"{d}: {card['name']}")
        return 0

    print(f"unknown mode {mode!r}; use push, daily or backfill")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
