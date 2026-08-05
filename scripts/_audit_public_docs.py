"""Audit the public docs against the rules they must hold to.

Run before publishing. Checks, in order of how badly each would land if it
reached a miner:

  1. NAMES — no personal names anywhere in public docs.
  2. INTERNAL NOTES — no unfinished-work markers, ticket references, or
     private working language that only makes sense inside the team.
  3. DATE CONSISTENCY — every doc that names the first live basket agrees,
     and agrees with the code constant.
  4. DEAD LINKS — every relative markdown link resolves to a real file.
  5. STALE ERA — weekly-era instructions outside the archive must carry a
     marker, so a miner cannot mistake them for current.

Exits non-zero if anything fails, so it can gate a publish.
"""
import os
import re
import subprocess
import sys

DOCS = "docs"
ARCHIVE = os.path.join(DOCS, "archive")

# The list of real names and internal jargon is itself private: writing it
# down here would publish, in the public repo, exactly what this gate exists
# to keep out of the public repo. So it is read from an operator-local file
# (gitignored) or the environment, and the gate REFUSES TO RUN without it —
# a silent skip would report CLEAN while checking nothing.
#
#   scripts/.doc_publish_blocklist   one entry per line; '#' comments
#                                    plain line  -> personal name (word-boundary, case-insensitive)
#                                    're: ...'   -> regex for internal phrasing
#   SN21_DOC_BLOCKLIST               same content, newline- or comma-separated
#   SN21_DOC_BLOCKLIST_FILE          alternative path to the file
BLOCKLIST_FILE = os.environ.get(
    "SN21_DOC_BLOCKLIST_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".doc_publish_blocklist"),
)

# Phrases that are generic enough to name in public — they give nothing away.
INTERNAL = [
    r"\bTODO\([^)]*\)", r"\bFIXME\b", r"\bXXX\b",
    r"\bpending approval\b",
    r"\binternal only\b", r"\bdo not publish\b",
]
NAMES = []


def load_blocklist():
    """Return (names, extra_patterns) or exit non-zero if unavailable."""
    raw = os.environ.get("SN21_DOC_BLOCKLIST")
    if raw is None:
        if not os.path.exists(BLOCKLIST_FILE):
            print(
                "REFUSING TO RUN — no blocklist.\n\n"
                f"  Expected {BLOCKLIST_FILE} (gitignored) or $SN21_DOC_BLOCKLIST.\n"
                "  One entry per line: a bare name, or 're: <regex>' for a phrase.\n"
                "  Without it this gate would print CLEAN while checking nothing."
            )
            sys.exit(2)
        raw = open(BLOCKLIST_FILE).read()
    names, patterns = [], []
    for line in raw.replace(",", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("re:"):
            patterns.append(line[3:].strip())
        else:
            names.append(line)
    if not names and not patterns:
        print(f"REFUSING TO RUN — blocklist at {BLOCKLIST_FILE} is empty.")
        sys.exit(2)
    return names, patterns

FIRST_BASKET_DATE = "2026-08-03"     # action-window day of the first live basket
FIRST_DELIVERY = "4 August"          # the day it reached miners

failures = []


def live_docs():
    """Tracked markdown outside the archive. TRACKED is the point: local-only
    drafts sit in docs/ too, and auditing those reports failures for text that
    was never going to be published."""
    tracked = set(subprocess.run(["git", "ls-files"],
                                 capture_output=True, text=True).stdout.split())
    out = []
    for root, dirs, files in os.walk(DOCS):
        if os.path.abspath(root).startswith(os.path.abspath(ARCHIVE)):
            continue
        for f in files:
            p = os.path.join(root, f)
            if f.endswith(".md") and p in tracked:
                out.append(p)
    for f in ("README.md", "CONTRIBUTING.md"):
        if f in tracked:
            out.append(f)
    return sorted(out)


# Source is a public surface too — the miner docs send readers straight into
# hope/scoring/. Comments there reach exactly the same audience as the docs,
# so they are held to the same rule.
CODE_SUFFIXES = (".py", ".sql", ".sh", ".yml", ".yaml", ".toml")


def tracked_code():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return sorted(
        p for p in out.stdout.split()
        if p.endswith(CODE_SUFFIXES) and os.path.exists(p)
    )


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
    names, extra = load_blocklist()
    NAMES.extend(names)
    INTERNAL.extend(extra)
    files = live_docs()
    code = tracked_code()
    print(f"auditing {len(files)} live docs + {len(code)} tracked source files "
          f"against {len(NAMES)} names + {len(INTERNAL)} phrase patterns "
          f"(archive excluded)\n")
    for path in files:
        with open(path) as f:
            text = f.read()
        check_names(path, text)
        check_internal(path, text)
        check_links(path, text)
        check_dates(path, text)
        check_era(path, text)

    # Source files get the names + internal-phrasing checks only: dates,
    # links and era markers are documentation concerns.
    for path in code:
        try:
            with open(path) as f:
                text = f.read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        check_names(path, text)
        check_internal(path, text)

    if not failures:
        print("CLEAN — no names or internal notes in docs OR source, links "
              "resolve, dates consistent, weekly-era material marked")
        return 0
    print(f"{len(failures)} issue(s):\n")
    for f in failures:
        print("  " + f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
