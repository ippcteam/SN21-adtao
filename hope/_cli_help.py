"""Argparse helper: a HelpFormatter that doesn't crash on literal ``%``.

argparse runs ``help_text % action.__dict__`` to substitute defaults via the
``%(default)s`` / ``%(prog)s`` pattern. A bare ``%`` in a help string followed
by something other than ``(`` (e.g. ``"60% null penalty"`` → ``%n``) crashes
with ``ValueError: unsupported format character 'n' (0x6e)`` before the
user even sees ``--help``.

Use this formatter on every CLI in the repo so adding a literal ``%`` to a
help string can't break ``--help`` for a contributor who forgets to write
``%%``. ``%(default)s``-style substitutions still work.
"""

from __future__ import annotations

import argparse
import re


_PCT_NOT_FOLLOWED_BY_PAREN = re.compile(r"%(?!\()")


class SafeHelpFormatter(argparse.HelpFormatter):
    """HelpFormatter that escapes bare ``%`` in help strings.

    ``%(name)s`` substitutions are preserved; everything else with a bare
    ``%`` is escaped so argparse's % formatting machinery treats it as a
    literal.
    """

    def _format_action(self, action):
        if action.help:
            original = action.help
            action.help = _PCT_NOT_FOLLOWED_BY_PAREN.sub("%%", original)
            try:
                return super()._format_action(action)
            finally:
                action.help = original
        return super()._format_action(action)
