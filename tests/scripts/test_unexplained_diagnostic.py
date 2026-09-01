"""The unfunded-with-no-reason diagnostic must name a row it is complaining about.

WHY THIS EXISTS
    The warning samples a hotkey from three key spaces so a reader can tell
    "nobody was acted on" apart from "the lookup missed" — the failure mode
    where one identity is written two ways and every lookup silently returns
    nothing.

    The report-side sample was taken from `miner_results[0]`: the first row of
    the report, which on a healthy day is a funded miner. So the message
    pointed at a hotkey that was not unexplained, was not involved, and led
    whoever read it to the wrong miner. A diagnostic that names an unrelated
    identity is worse than one that names none, because it is trusted.

    Pinned here: the sampled hotkey always comes from an unexplained row.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "run_daily_pipeline.py"


def _publish_source() -> str:
    src = SOURCE.read_text()
    start = src.index("_unexplained")
    end = src.index("post_with_correction", start)
    return src[start:end]


def test_the_sample_comes_from_an_unexplained_row():
    body = _publish_source()
    assert "_unexplained_rows[0].hotkey" in body, (
        "the report-side sample must be drawn from a row that is actually "
        "unfunded-with-no-reason")


def test_it_no_longer_samples_the_first_report_row():
    body = _publish_source()
    assert "miner_results[0].hotkey" not in body, (
        "miner_results[0] is the first row of the report — usually a funded "
        "miner — and naming it sends the reader to an unrelated identity")


def test_the_count_and_the_sample_come_from_the_same_set():
    """Counting one set and sampling another is how the two drifted apart."""
    body = _publish_source()
    assert "_unexplained = len(_unexplained_rows)" in body


def test_all_three_key_spaces_are_still_named():
    """The whole point is comparing spaces; losing one loses the diagnosis."""
    body = _publish_source()
    for space in ("report hotkey=", "audit hotkey=", "earning hotkey="):
        assert space in body


def test_the_warning_only_fires_when_there_is_something_to_report():
    body = _publish_source()
    assert re.search(r"if _unexplained:", body)


def test_empty_sets_do_not_crash_the_warning():
    """A day where the audit or earning set is empty must still log, not
    raise — the warning exists precisely for the days that look wrong."""
    body = _publish_source()
    assert "if _audit else 'none'" in body
    assert "if _earning else 'none'" in body
