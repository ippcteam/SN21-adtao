"""Model intake sweep — the production caller `run_intake` never had.

    python3 scripts/run_model_intake.py \
        --ledger-root /var/lib/sn21_ledger \
        --labelled-export /var/lib/sn21/labelled_episodes.jsonl \
        --published-bundle /var/lib/sn21/SN21_training_bundle.jsonl \
        [--ed25519-key-file KEY] [--limit N] [--dry-run]

WHAT IT IS FOR

    Miners commit `sn21-model:v1:<repo>@sha256:<digest>` on chain. Until a
    digest is gated and admitted it is not runnable, and `build_registry`
    will not put it in any daily basket. This sweep is the step that turns a
    commitment into a verdict.

    Every piece it calls — the registry parser, the digest-pinned pull, the
    gate, the verdict rail — already existed and was tested. None of them had
    a caller, so the quickstart's promise that "the subnet pulls, gate-admits
    and runs it" had nothing behind it.

DRY RUN NEEDS NO DOCKER

    `--dry-run` reads the chain, parses commitments and reports exactly which
    digests WOULD be gated, without pulling or executing anything. That makes
    the chain-facing half checkable on any machine, and leaves only the
    container execution needing the docker host.

RUNNING STRANGERS' CODE IS THE EXPENSIVE STEP

    Work is keyed on the DIGEST and skipped when a final verdict exists, so
    re-running the sweep is cheap. `--limit` bounds a single sweep so the
    first production run cannot be a hundred untrusted container starts in
    one unattended go.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest import heldout_corpus  # noqa: E402
from hope.backtest.chain_commitments import (  # noqa: E402
    as_single_reader,
    bulk_model_commitments,
)
from hope.backtest.gate_service import gate_submission  # noqa: E402
from hope.backtest.intake_runner import (  # noqa: E402
    admitted_path,
    commitments_from_chain,
    run_intake,
    verdict_dir,
    verdicted_digests,
)
from hope.backtest.model_registry import MODEL_COMMIT_PREFIX  # noqa: E402
from hope.commitment.chain_reader import read_commitment_of  # noqa: E402


def _parse_args(argv):
    p = argparse.ArgumentParser(description="SN21 model intake sweep")
    p.add_argument("--network", default=os.environ.get("SN21_NETWORK", "finney"))
    p.add_argument("--netuid", type=int,
                   default=int(os.environ.get("SN21_NETUID", "21")))
    p.add_argument("--ledger-root", required=True)
    p.add_argument("--labelled-export",
                   default=os.environ.get("SN21_LABELLED_EXPORT"))
    p.add_argument("--published-bundle",
                   default=os.environ.get("SN21_PUBLISHED_BUNDLE"))
    p.add_argument("--cutoff", default=os.environ.get("SN21_CORPUS_CUTOFF"),
                   help="override the published-bundle cutoff date")
    p.add_argument("--corpus-size", type=int,
                   default=heldout_corpus.DEFAULT_CORPUS_EPISODES)
    p.add_argument("--ed25519-key-file",
                   default=os.environ.get("SN21_ED25519_KEY_FILE"))
    p.add_argument("--limit", type=int, default=0,
                   help="max digests to gate this sweep (0 = no bound)")
    p.add_argument("--timeout-s", type=int, default=15 * 60)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be gated; pull/execute nothing")
    return p.parse_args(argv)


def _load_key(path):
    """The attestation key, or None. A missing key is not fatal: an
    unattested verdict is still a verdict, and refusing to gate because we
    cannot sign would stall admission over a publication concern."""
    if not path or not os.path.exists(path):
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    with open(path, "rb") as f:
        raw = f.read().strip()
    try:
        raw = bytes.fromhex(raw.decode().strip())
    except (ValueError, UnicodeDecodeError):
        pass
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def _chain_reader(subtensor, netuid, prefetched=None):
    """hotkey -> the raw model commitment string, or None.

    `prefetched` is the bulk reader when one is available. Without it this
    falls back to the per-hotkey contract in `chain_reader`, which decodes
    properly typed fields and is what the rest of the system tests against.

    Only fields that decode to UTF-8 and carry our prefix are returned; the
    chain slot is shared and last-write-wins, so a hotkey may legitimately
    hold something else entirely.
    """
    def read(hotkey):
        if prefetched is not None:
            return prefetched(hotkey)
        try:
            fields = read_commitment_of(subtensor, netuid, hotkey)
        except Exception:
            return None
        for field in fields or []:
            try:
                text = field.bytes_.decode("utf8").strip()
            except (UnicodeDecodeError, AttributeError):
                continue
            if text.startswith(MODEL_COMMIT_PREFIX):
                return text
        return None
    return read


def _persist(ledger_root):
    """Write each verdict, and keep the admitted set in step.

    The admitted file is rewritten from the verdicts on disk rather than
    appended to, so a digest that a later sweep rejects cannot linger in the
    runnable set because of a line written weeks ago.
    """
    directory = verdict_dir(ledger_root)
    os.makedirs(directory, exist_ok=True)

    def persist(digest, record):
        safe = (digest or "unknown").replace(":", "_").replace("/", "_")
        path = os.path.join(directory, f"{safe}.json")
        with open(path + ".tmp", "w") as f:
            json.dump({"image_digest": digest, **record}, f, indent=1)
        os.replace(path + ".tmp", path)
    return persist


def _rewrite_admitted(ledger_root):
    """Rebuild _admitted_digests.json from the verdict files on disk."""
    directory = verdict_dir(ledger_root)
    admitted = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            try:
                with open(os.path.join(directory, name)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if rec.get("status") == "admitted":
                digest = rec.get("image_digest") or rec.get("digest")
                if digest:
                    admitted.append(digest)
    path = admitted_path(ledger_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump({"admitted": sorted(set(admitted)),
                   "count": len(set(admitted))}, f, indent=1)
    os.replace(path + ".tmp", path)
    return sorted(set(admitted))


def main(argv=None):
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    import bittensor as bt
    subtensor = bt.Subtensor(network=args.network)
    metagraph = subtensor.metagraph(netuid=args.netuid)
    hotkeys = list(metagraph.hotkeys)
    print(f"[intake] {len(hotkeys)} registered hotkeys on netuid {args.netuid}",
          flush=True)

    bulk = bulk_model_commitments(subtensor, args.netuid, hotkeys)
    if bulk:
        print(f"[intake] bulk read: {len(bulk)} model commitments", flush=True)
    else:
        print("[intake] bulk read unavailable — per-hotkey fallback (slow)",
              flush=True)
    read = _chain_reader(subtensor, args.netuid,
                         as_single_reader(bulk) if bulk else None)
    commitments, unparseable = commitments_from_chain(hotkeys, read)
    already = verdicted_digests(args.ledger_root)
    pending = [c for c in commitments if c.digest not in already]

    print(f"[intake] model commitments : {len(commitments)}", flush=True)
    print(f"[intake] unusable/malformed: {unparseable}", flush=True)
    print(f"[intake] already verdicted : {len(commitments) - len(pending)}",
          flush=True)
    print(f"[intake] pending gate      : {len(pending)}", flush=True)

    if args.dry_run:
        by_repo = {}
        for c in pending:
            by_repo.setdefault(c.image_ref, []).append(c.hotkey)
        print("\n[intake] WOULD gate, by repository:")
        for repo, hks in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(hks):>4}  {repo}")
        print("\n[intake] dry run — nothing pulled, nothing executed")
        return 0

    if not pending:
        print("[intake] nothing to do")
        _rewrite_admitted(args.ledger_root)
        return 0

    if not args.labelled_export:
        print("[intake] ERROR: --labelled-export is required to gate")
        return 2

    corpus = heldout_corpus.build(args.labelled_export,
                                  bundle_path=args.published_bundle,
                                  cutoff=args.cutoff,
                                  limit=args.corpus_size)
    print(f"[intake] corpus: {corpus['episode_count']} held-out episodes "
          f"after {corpus['cutoff']}, {corpus['outcome_row_count']} truth rows",
          flush=True)

    key = _load_key(args.ed25519_key_file)
    if key is None:
        print("[intake] no attestation key — verdicts will be unsigned",
              flush=True)

    if args.limit and len(pending) > args.limit:
        # Bounded deliberately: the sweep is idempotent, so the remainder is
        # simply picked up next run rather than lost.
        keep = {c.digest for c in
                sorted(pending, key=lambda c: c.hotkey)[:args.limit]}
        print(f"[intake] limiting this sweep to {args.limit} digests "
              f"({len(pending) - args.limit} deferred to the next run)",
              flush=True)
    else:
        keep = None

    # The executor has no docker daemon, so admission must run each image
    # through the same namespace sandbox the shadow day uses. One resolver,
    # chosen by SN21_EXECUTOR_MODE; default docker keeps every other caller
    # unchanged.
    from hope.backtest.execution_mode import basket_runner, executor_mode
    run_basket = basket_runner()
    print(f"[intake] executor mode: {executor_mode()}", flush=True)

    def _gate_exec(pinned_ref, episodes, timeout_s):
        # gate_service passes a timeout the sandbox runner does not take.
        return run_basket(pinned_ref, episodes)

    gated = {"n": 0}

    def gate_runner(local_ref):
        gated["n"] += 1
        print(f"[intake]   gating {gated['n']}: {local_ref}", flush=True)
        return gate_submission(
            local_ref, corpus["episodes"], corpus["outcomes"],
            generated_at=now, private_key=key, timeout_s=args.timeout_s,
            runner=_gate_exec)

    def read_filtered(hotkey):
        raw = read(hotkey)
        if raw is None or keep is None:
            return raw
        from hope.backtest.model_registry import parse_model_commitment
        parsed = parse_model_commitment(raw)
        if parsed and parsed.get("digest") in keep:
            return raw
        return None

    result = run_intake(
        args.ledger_root, hotkeys,
        read_filtered if keep else read,
        gate_runner, persist=_persist(args.ledger_root))

    admitted = _rewrite_admitted(args.ledger_root)

    print("\n[intake] " + "=" * 50)
    print(json.dumps(result.as_dict()
                     | {"admitted_total_on_file": len(admitted)},
                     indent=1, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
