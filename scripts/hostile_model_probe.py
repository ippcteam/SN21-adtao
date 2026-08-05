#!/usr/bin/env python3
"""Push deliberately broken miner models through the REAL execution path.

Everything upstream of this has only ever run our own model. We have tested
the pipe, not the traffic. This builds genuinely defective container images,
runs them through run_basket_docker with the production isolation flags, and
checks the fault lands on the right party — because the failure that matters
is not "a model broke", it is "a model broke and we struck the wrong person".

That is not hypothetical. A docker-registry outage was being recorded as the
miner's failure until 2026-08-01; the marker collided with the crash marker.

Cases, and what each SHOULD produce:
  crash    non-zero exit          -> exit=      -> FAULT_MINER   (their code)
  hang     exceeds the time budget-> timeout>   -> FAULT_MINER   (their budget)
  memory   exceeds 1g, OOM-killed -> exit=137   -> FAULT_MINER   (their budget)
  garbage  valid run, junk stdout -> ok=True    -> FAULT_NONE    (ran; earns nothing)
  good     control                -> ok=True    -> FAULT_NONE    (+ predictions)

The garbage case is the interesting one: _parse_output ignores non-JSON as
"never fatal", so a model emitting rubbish is a SUCCESSFUL run with zero
predictions. It is not struck — it simply earns nothing, and the
evidence-weighted standing does the punishing. That is defensible, but it is
a policy choice rather than an accident, so it is asserted here explicitly.

Requires docker. Builds throwaway images, removes them after. Exit 0 = every
fault landed on the right party.

    python3 scripts/hostile_model_probe.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hope.backtest.container_runner import (
    ERR_EXIT_PREFIX,
    ERR_TIMEOUT_PREFIX,
    run_basket_docker,
)
from hope.scoring.chronic_failure import (
    FAULT_MINER,
    FAULT_NONE,
    classify_failure,
)

TAG = "sn21-hostile"
FAILS = []

# Each model reads stdin (the basket) and misbehaves in exactly one way.
MODELS = {
    "crash": "import sys\nsys.stdin.read()\nsys.exit(3)\n",
    "hang": "import sys,time\nsys.stdin.read()\ntime.sleep(600)\n",
    # ONE huge allocation, not a slow loop. The first cut grew 64MB at a time
    # and the time budget expired before the cgroup OOM-killer fired — the
    # fault still landed on the miner, but via timeout> rather than exit=137,
    # so the OOM path went untested while the probe looked like it passed.
    "memory": ("import sys\nsys.stdin.read()\n"
               "buf = bytearray(4 * 1024 * 1024 * 1024)\n"
               "print(len(buf))\n"),
    "garbage": ("import sys\nsys.stdin.read()\n"
                "print('not json at all')\nprint('{{{ broken')\n"
                "print('<html>whoops</html>')\n"),
    "good": ("import sys,json\n"
             "for line in sys.stdin:\n"
             "    line=line.strip()\n"
             "    if not line: continue\n"
             "    e=json.loads(line)\n"
             "    q={'p10':-0.1,'p50':0.0,'p90':0.1}\n"
             "    print(json.dumps({'episode_id':e['episode_id'],'horizons':{\n"
             "        '7':{'cost_delta_pct':q,'conversions_delta_pct':q,"
             "'efficiency_delta_pct':q}}}))\n"),
}

EXPECT = {
    "crash":   (False, ERR_EXIT_PREFIX,    FAULT_MINER),
    "hang":    (False, ERR_TIMEOUT_PREFIX, FAULT_MINER),
    "memory":  (False, ERR_EXIT_PREFIX,    FAULT_MINER),
    "garbage": (True,  None,               FAULT_NONE),
    "good":    (True,  None,               FAULT_NONE),
}

EPISODES = [{"episode_id": f"ep{i}", "input": {"x": i}} for i in range(3)]


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def build(name, src):
    d = tempfile.mkdtemp(prefix=f"sn21_{name}_")
    try:
        with open(os.path.join(d, "model.py"), "w") as f:
            f.write(src)
        with open(os.path.join(d, "Dockerfile"), "w") as f:
            f.write("FROM python:3.12-alpine\n"
                    "COPY model.py /model.py\n"
                    'ENTRYPOINT ["python3","-u","/model.py"]\n')
        r = subprocess.run(["docker", "build", "-q", "-t", f"{TAG}-{name}", d],
                           capture_output=True, timeout=600,
                           check=False)
        if r.returncode != 0:
            print(f"  build failed for {name}: {r.stderr.decode()[:300]}")
            return False
        return True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        print("docker is not available — cannot run the real path", file=sys.stderr)
        return 2

    print("HOSTILE MODEL PROBE — real containers, real runner, real isolation flags\n")
    print("building 5 throwaway images...")
    for name, src in MODELS.items():
        if not build(name, src):
            return 3
    print("built.\n")

    for name in MODELS:
        want_ok, want_marker, want_fault = EXPECT[name]
        # short budgets so the probe is fast; the SHAPE of the failure is what
        # matters, not the wall-clock size of the budget
        # the hang case needs a SHORT clock; the memory case needs a long
        # enough one that the OOM-killer, not the timeout, is what fires.
        res = run_basket_docker(f"{TAG}-{name}", EPISODES,
                                timeout_s=8 if name != "memory" else 90,
                                memory="512m")
        fault = classify_failure(res.ok, res.error)
        print(f"{name}:")
        print(f"    ok={res.ok}  preds={res.predictions_out}/{res.episodes_in}  "
              f"error={(res.error or '')[:70]!r}")
        check(f"{name}: ok is {want_ok}", res.ok == want_ok)
        if want_marker:
            check(f"{name}: error carries {want_marker!r}",
                  (res.error or "").startswith(want_marker), res.error or "")
        check(f"{name}: fault attributed to {want_fault}",
              fault == want_fault, f"got {fault}")

    # the case that would be invisible: garbage must NOT be a strike, but it
    # must also not earn — a run with no predictions carries no evidence.
    res = run_basket_docker(f"{TAG}-garbage", EPISODES, timeout_s=8, memory="512m")
    check("garbage earns nothing (zero predictions parsed)",
          res.predictions_out == 0, f"{res.predictions_out} predictions")

    for name in MODELS:
        subprocess.run(["docker", "rmi", "-f", f"{TAG}-{name}"], capture_output=True, check=False)

    print("\n" + "=" * 64)
    if FAILS:
        print(f"PROBE FAILED — {len(FAILS)}: {FAILS}")
        return 1
    print("PROBE CLEAN — every failure landed on the right party.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
