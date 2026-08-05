"""Audit the public docs against the rules they must hold to.

Run before publishing. Checks, in order of how badly each would land if it
reached a miner:

  1. NAMES — no personal names anywhere in public docs.
  2. INTERNAL NOTES — no "ask X", "TODO(name)", ticket refs, or private
     working language that only makes sense inside the team.
  3. DATE CONSISTENCY — every doc that names the first live basket agrees,
     and agrees with the code constant.
  4. DEAD LINKS — every relative markdown link resolves to a real file.
  5. STALE ERA — weekly-era instructions outside the archive must carry a
     marker, so a miner cannot mistake them for current.

Exits non-zero if anything fails, so it can gate a publish.
"""
import os
import re
import sys

DOCS = "docs"
ARCHIVE = os.path.join(DOCS, "archive")

# Personal names must never appear in public docs. Substring-matched
# case-insensitively against word boundaries.
NAMES = ["rob", "jayesh", "khurram", "warner", "rabbia"]

# Phrases that mean "this was written for us, not for a reader".
INTERNAL = [
    r"\bTODO\([^)]*\)", r"\bFIXME\b", r"\bXXX\b",
    r"\bask (rob|jayesh|khurram)\b",
    r"\bpending (rob|approval)\b", r"\[PENDING ROB\]",
    r"\binternal only\b", r"\bdo not publish\b",
    r"\bK-Work\b", r"\bsession bus\b",
]

FIRST_BASKET_DATE = "2026-08-03"     # action-window day of the first live basket
FIRST_DELIVERY = "4 August"          # the day it reached miners

failures = []


def live_docs():
    out = []
    for root, dirs, files in os.walk(DOCS):
        if os.path.abspath(root).startswith(os.path.abspath(ARCHIVE)):
            continue
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.join(root, f))
    for f in ("README.md", "CONTRIBUTING.md"):
        if os.path.exists(f):
            out.append(f)
    return sorted(out)


def check_names(path, text):
    for name in NAMES:
        for m in re.finditer(rf"\b{name}\b", text, re.I):
            line = text[:m.start()].count("\n") + 1
            failures.append(f"NAME  {path}:{line} — '{m.group(0)}'")


def check_internal(path, text):
    for pat in INTERNAL:
        for m in re.finditer(pat, text, re.I):
            line = text[:m.start()].count("\n") + 1
            failures.append(f"INTERNAL  {path}:{line} — '{m.group(0)[:40]}'")


def check_links(path, text):
    for m in re.finditer(r"\]\((\.[^)#]*?\.md)(#[^)]*)?\)", text):
        target = os.path.normpath(os.path.join(os.path.dirname(path), m.group(1)))
        if not os.path.exists(target):
            line = text[:m.start()].count("\n") + 1
            failures.append(f"DEADLINK  {path}:{line} — {m.group(1)}")


def check_dates(path, text):
    """Any doc naming the first live basket must name BD-2026-08-03 and its
    4 August delivery together — the ambiguity that blocked publication was
    exactly one of these appearing without the other."""
    if re.search(r"first\s+(live\s+)?(daily\s+)?(basket|bundle)", text, re.I):
        has_key = FIRST_BASKET_DATE in text or "BD-2026-08-03" in text
        has_del = FIRST_DELIVERY in text or "4 Aug" in text
        if not (has_key and has_del):
            failures.append(
                f"DATE  {path} — names the first basket but not both its "
                f"coverage day ({FIRST_BASKET_DATE}) and delivery ({FIRST_DELIVERY})")


def check_era(path, text):
    weekly = re.search(r"weekly epoch|hope-miner --epoch|mining deadline", text, re.I)
    if not weekly:
        return
    head = text[:2000].lower()
    marked = any(w in head for w in
                 ("historical", "weekly era", "archived", "concluded",
                  "no longer", "obsolete", "pre-daily", "superseded"))
    # per-section markers count too
    inline = re.search(r"historical|weekly era|concluded", text, re.I)
    if not (marked or inline):
        failures.append(
            f"ERA  {path} — weekly-era instructions with no marker; a reader "
            f"cannot tell it is not current")


def main():
    files = live_docs()
    print(f"auditing {len(files)} live docs (archive excluded)\n")
    for path in files:
        with open(path) as f:
            text = f.read()
        check_names(path, text)
        check_internal(path, text)
        check_links(path, text)
        check_dates(path, text)
        check_era(path, text)

    if not failures:
        print("CLEAN — no names, no internal notes, links resolve, "
              "dates consistent, weekly-era material marked")
        return 0
    print(f"{len(failures)} issue(s):\n")
    for f in failures:
        print("  " + f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
