"""The one-seat cap must survive a transient chain read.

WHY THIS EXISTS
    The cap removes roughly eighty-five hotkeys from the earning set on a
    normal day, so its absence is the single largest change to who gets paid
    that one run can make. It is deliberately fail-open: an identity we could
    not read is not evidence that anyone is farming. But fail-open on ONE
    attempt is not a policy, it is a coin toss — a single websocket keepalive
    timeout disabled the cap for a whole day, and the run still published and
    reported clean.

    So the read retries, and these tests pin the parts that make that true:
    that a transient fault is survived, that a genuinely unavailable chain
    still fails open rather than guessing, and that an empty metagraph is
    treated as a bad read instead of a subnet with no miners.
"""

import sys
import types

import pytest

import scripts.run_daily_pipeline as entry


class _Metagraph:
    def __init__(self, hotkeys, coldkeys):
        self.hotkeys = hotkeys
        self.coldkeys = coldkeys


class _Subtensor:
    """Replays a scripted sequence of outcomes, one per read."""

    def __init__(self, network=None):
        self.network = network

    def metagraph(self, netuid):
        outcome = _Subtensor.script.pop(0)
        _Subtensor.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return _Metagraph(*outcome)


@pytest.fixture
def chain(monkeypatch):
    """Installs a fake `bittensor` and returns a scripting handle."""
    module = types.ModuleType("bittensor")
    module.Subtensor = _Subtensor
    monkeypatch.setitem(sys.modules, "bittensor", module)
    monkeypatch.setenv("SN21_NETUID", "21")
    monkeypatch.setenv("BT_NETWORK", "finney")

    def script(*outcomes):
        _Subtensor.script = list(outcomes)
        _Subtensor.calls = 0
        return _Subtensor

    return script


GOOD = (["hk1", "hk2"], ["ck1", "ck1"])


def _no_sleep(_seconds):
    _no_sleep.slept.append(_seconds)


def _fresh_sleep():
    _no_sleep.slept = []
    return _no_sleep


class TestTransientFaults:
    def test_a_read_that_fails_once_still_produces_the_cap(self, chain):
        """The exact shape of the incident: one keepalive timeout, then a
        perfectly healthy chain."""
        sub = chain(ConnectionError("keepalive ping timeout"), GOOD)
        out = entry._coldkey_reader(sleep=_fresh_sleep())
        assert out == {"hk1": "ck1", "hk2": "ck1"}
        assert sub.calls == 2

    def test_it_survives_failures_up_to_the_last_attempt(self, chain):
        sub = chain(ConnectionError("boom"), ConnectionError("boom"), GOOD)
        assert entry._coldkey_reader(sleep=_fresh_sleep()) == {"hk1": "ck1",
                                                              "hk2": "ck1"}
        assert sub.calls == 3

    def test_it_waits_longer_between_attempts(self, chain):
        """Backing off matters: three reads a second apart during a chain
        hiccup are one read, tried three times."""
        chain(ConnectionError("a"), ConnectionError("b"), GOOD)
        sleep = _fresh_sleep()
        entry._coldkey_reader(backoff_s=5, sleep=sleep)
        assert _no_sleep.slept == [5, 10]

    def test_a_successful_first_read_never_sleeps(self, chain):
        chain(GOOD)
        sleep = _fresh_sleep()
        entry._coldkey_reader(sleep=sleep)
        assert _no_sleep.slept == []


class TestStillFailsOpen:
    def test_a_chain_that_is_really_down_disables_the_cap(self, chain):
        """Retrying must not turn fail-open into fail-closed. Nobody loses a
        seat because our own read was broken."""
        sub = chain(*[ConnectionError("down")] * 3)
        assert entry._coldkey_reader(sleep=_fresh_sleep()) is None
        assert sub.calls == 3

    def test_it_does_not_retry_forever(self, chain):
        chain(*[ConnectionError("down")] * 10)
        entry._coldkey_reader(attempts=3, sleep=_fresh_sleep())
        assert _Subtensor.calls == 3


class TestEmptyIsNotSuccess:
    def test_an_empty_metagraph_is_retried_not_believed(self, chain):
        """A metagraph with no hotkeys is a bad read, not a subnet with no
        miners. Believing it would let the cap 'run' over nothing and report
        success — which is how a missing control hides."""
        sub = chain(([], []), GOOD)
        assert entry._coldkey_reader(sleep=_fresh_sleep()) == {"hk1": "ck1",
                                                              "hk2": "ck1"}
        assert sub.calls == 2

    def test_a_persistently_empty_metagraph_disables_the_cap(self, chain):
        chain(([], []), ([], []), ([], []))
        assert entry._coldkey_reader(sleep=_fresh_sleep()) is None


class TestAHungReadIsNotAWaitForever:
    """A hung read is not a slow read.

    `bt.Subtensor().metagraph()` has no timeout of its own. When the websocket
    stalls instead of raising, it blocks forever — and a retry loop that only
    catches EXCEPTIONS never gets a turn. That happened in production: the
    settle stage went silent mid-run and the day's vector could not publish at
    all. Failing open requires the read to fail FIRST, so it is given a
    deadline.
    """

    def test_a_read_that_never_returns_is_abandoned(self, chain, monkeypatch):
        import threading

        release = threading.Event()

        class Hanging:
            calls = 0

            def __init__(self, network=None):
                pass

            def metagraph(self, netuid):
                Hanging.calls += 1
                release.wait(30)          # never set during the test
                return _Metagraph(*GOOD)

        import sys
        sys.modules["bittensor"].Subtensor = Hanging
        try:
            out = entry._coldkey_reader(attempts=2, timeout_s=0.2,
                                        sleep=_fresh_sleep())
            assert out is None, "a hung chain must fail open, not block"
            assert Hanging.calls == 2, "each attempt must get its own deadline"
        finally:
            release.set()

    def test_a_slow_but_finishing_read_is_allowed(self, chain, monkeypatch):
        """The deadline must not punish a chain that is merely slow."""
        import time as _t

        class Slow:
            def __init__(self, network=None):
                pass

            def metagraph(self, netuid):
                _t.sleep(0.05)
                return _Metagraph(*GOOD)

        import sys
        sys.modules["bittensor"].Subtensor = Slow
        assert entry._coldkey_reader(timeout_s=5, sleep=_fresh_sleep()) == {
            "hk1": "ck1", "hk2": "ck1"}

    def test_the_pool_is_not_shut_down_with_wait(self):
        """`with ThreadPoolExecutor(...)` calls shutdown(wait=True) on exit,
        which blocks until the hung worker finishes — reinstating the exact
        hang the timeout exists to escape."""
        import inspect
        # Strip comments first: the function DOCUMENTS why it avoids the
        # context manager, and a naive grep matches that prose instead of the
        # code — a test that reads explanations rather than behaviour.
        code = "\n".join(
            line for line in inspect.getsource(entry._coldkey_reader).splitlines()
            if not line.strip().startswith("#"))
        assert "shutdown(wait=False)" in code
        assert "with ThreadPoolExecutor" not in code
