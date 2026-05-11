"""SN21 ed25519 key-management CLI.

Miners, validators, and the outcome signer each need a 32-byte ed25519
signing key for the protocol's `inner_sig` (Layer 9.A.1 / 9.B / 9.C.1 / 9.C.2).

Bittensor SS58 hotkeys are typically sr25519. SN21 cannot reuse them
directly for inner_sig because the protocol fixes ed25519 (drand-compatible,
cleanly available in `cryptography`, deterministic). This CLI handles the
ed25519 key lifecycle:

  generate     — create a new ed25519 keypair, write PEM to disk (0600)
  show         — print the ed25519 public key (32-byte hex) of a stored key
  sign-test    — sign a test message and verify, proving the key works
  register     — submit the on-chain Raw{N} commit binding the SS58 hotkey
                 to this ed25519 pubkey (Phase D F-3 / hope/commitment/registration.py)
  verify-reg   — read the on-chain registration for an SS58 hotkey and
                 verify it parses + the signature is valid

Usage:

  python scripts/sn21_keys.py generate \\
      --role miner --output ~/.sn21/miner-ed25519.pem
  python scripts/sn21_keys.py show --key ~/.sn21/miner-ed25519.pem
  python scripts/sn21_keys.py sign-test --key ~/.sn21/miner-ed25519.pem

  python scripts/sn21_keys.py register \\
      --role miner --netuid 21 --network finney \\
      --wallet-name my-miner --wallet-hotkey default \\
      --key ~/.sn21/miner-ed25519.pem

  python scripts/sn21_keys.py verify-reg \\
      --netuid 21 --network finney \\
      --hotkey-ss58 5Hoo2cRURm8A36Wup...

The PEM file is the SECRET. Back it up offline (cold storage). Loss = the
miner cannot inner-sign predictions and would need to register a new key
(causing a one-tempo gap before the new key is verifiable).
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Make the hope/ package importable when run as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Note: `hope.commitment.registration` triggers a top-level `import bittensor`
# (via the `hope.commitment` package __init__), and bittensor intercepts
# `--help` in sys.argv at import time. So we defer the registration import
# to inside the cmd functions that actually need it. The CLI's `--help`,
# `generate`, `show`, and `sign-test` paths run with no chain dependency.

_ROLE_VALUES = ("miner", "validator", "outcome_signer")


def _registration_role(name: str):
    """Lazy import + lookup of RegistrationRole by name."""
    from hope.commitment.registration import RegistrationRole
    return {
        "miner": RegistrationRole.MINER,
        "validator": RegistrationRole.VALIDATOR,
        "outcome_signer": RegistrationRole.OUTCOME_SIGNER,
    }[name]


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        raise SystemExit(f"key file not found: {path}")
    raw = path.read_bytes()
    sk = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(sk, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an ed25519 private key (got {type(sk).__name__})")
    return sk


def _save_private_key(sk: Ed25519PrivateKey, path: Path) -> None:
    pem = sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _ss58_to_raw_pubkey(ss58_address: str) -> bytes:
    """Decode an SS58 address to 32 raw public-key bytes."""
    try:
        from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
    except Exception:
        try:
            from substrateinterface import Keypair  # type: ignore
        except Exception as e:
            raise SystemExit(f"no Keypair available: {e}") from e
    kp = Keypair(ss58_address=ss58_address)
    pk = getattr(kp, "public_key", None)
    if pk is None:
        raise SystemExit("Keypair has no public_key attribute")
    if isinstance(pk, str):
        pk = bytes.fromhex(pk[2:] if pk.startswith("0x") else pk)
    if not isinstance(pk, (bytes, bytearray)) or len(pk) != 32:
        raise SystemExit(f"unexpected public_key shape: {type(pk).__name__}")
    return bytes(pk)


# ============================================================================
# Subcommands
# ============================================================================


def cmd_generate(args) -> int:
    """Create a fresh ed25519 keypair and save to PEM."""
    out = Path(args.output).expanduser()
    if out.exists() and not args.force:
        print(f"ERROR: {out} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    if args.role not in _ROLE_VALUES:
        raise SystemExit(f"unknown role: {args.role}")
    sk = Ed25519PrivateKey.generate()
    _save_private_key(sk, out)
    pk = sk.public_key().public_bytes_raw()
    print(f"role:    {args.role}")
    print(f"pubkey:  {pk.hex()}")
    print(f"saved:   {out}  (mode 0600)")
    return 0


def cmd_show(args) -> int:
    """Print the public key (32-byte hex) of a stored ed25519 PEM."""
    sk = _load_private_key(Path(args.key).expanduser())
    pk = sk.public_key().public_bytes_raw()
    print(pk.hex())
    return 0


def cmd_sign_test(args) -> int:
    """Round-trip a sign+verify on a test message — proves the key file is intact."""
    sk = _load_private_key(Path(args.key).expanduser())
    pk = sk.public_key().public_bytes_raw()
    msg = b"sn21-key-self-test-v1"
    sig = sk.sign(msg)
    if len(sig) != 64:
        print(f"ERROR: unexpected sig length {len(sig)}", file=sys.stderr)
        return 1
    Ed25519PublicKey.from_public_bytes(pk).verify(sig, msg)
    print(f"ok  pubkey={pk.hex()[:16]}...  sig_len={len(sig)}")
    return 0


def cmd_register(args) -> int:
    """Submit the on-chain Raw{N} commit binding (hotkey, role, ed25519_pk)."""
    from hope.commitment.registration import build_registration_payload
    role = _registration_role(args.role)
    sk = _load_private_key(Path(args.key).expanduser())

    try:
        import bittensor as bt  # type: ignore
    except ImportError:
        print("ERROR: bittensor not installed", file=sys.stderr)
        return 2

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    ss58 = wallet.hotkey.ss58_address
    ss58_pubkey = _ss58_to_raw_pubkey(ss58)
    payload = build_registration_payload(
        role=role, ss58_pubkey=ss58_pubkey, ed25519_signing_key=sk,
    )
    print(f"role:        {args.role}")
    print(f"ss58:        {ss58}")
    print(f"ed25519 pk:  {sk.public_key().public_bytes_raw().hex()}")
    print(f"payload:     {len(payload)} bytes ({payload.hex()[:40]}...)")
    print(f"network:     {args.network}")
    print(f"netuid:      {args.netuid}")
    print()
    # Auto-confirm when stdin isn't an interactive terminal (cron, CI,
    # docker run, etc.) — otherwise the script silently aborts on an
    # empty readline(), which looks like a hang to a scripted miner.
    if not args.yes and sys.stdin.isatty():
        sys.stderr.write("Submit registration commit? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() != "y":
            print("aborted.")
            return 1
    elif not args.yes:
        print("(stdin is not a TTY — auto-confirming. Pass --yes to silence this.)")

    st = bt.Subtensor(network=args.network)
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic
    response = publish_metadata_extrinsic(
        subtensor=st, wallet=wallet, netuid=args.netuid,
        data_type=f"Raw{len(payload)}",
        data=payload,
        wait_for_inclusion=True,
        wait_for_finalization=True,
        raise_error=False,
    )
    success = bool(getattr(response, "success", False))
    msg = str(getattr(response, "message", ""))[:200]
    print(f"success: {success}")
    print(f"message: {msg}")
    if success:
        # Surface the block + extrinsic hash so the miner can later pass
        # --block-hash to scripts/verify_epoch.py for a block-pinned read
        # of the registration (the Commitments slot gets overwritten by
        # the first hope-miner bundle, so head-of-chain reads won't find
        # the registration after that point).
        block = None
        for path in [
            ("block_number",), ("block_num",), ("block",),
            ("extrinsic_receipt", "block_number"),
            ("extrinsic_receipt", "block_num"),
        ]:
            obj = response
            for attr in path:
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if obj is not None:
                block = obj
                break
        if block is None:
            try:
                commit = st.substrate.query(
                    "Commitments", "CommitmentOf",
                    [args.netuid, wallet.hotkey.ss58_address],
                )
                v = commit.value if hasattr(commit, "value") else commit
                if isinstance(v, dict):
                    block = v.get("block")
            except Exception:
                pass
        if block is not None:
            print(f"block: {block}")
        ext_hash = getattr(response, "extrinsic_hash", None) or getattr(
            getattr(response, "extrinsic_receipt", None) or object(),
            "extrinsic_hash", None,
        )
        if ext_hash:
            if isinstance(ext_hash, (bytes, bytearray)):
                ext_hash = "0x" + ext_hash.hex()
            print(f"extrinsic_hash: {ext_hash}")
    return 0 if success else 3


def cmd_verify_reg(args) -> int:
    """Read the on-chain Raw{N} commit for an SS58 and verify it.

    Goes directly to substrate (Commitments::CommitmentOf) to avoid the
    Bittensor SDK's lossy UTF-8 decode of binary payloads.
    """
    from hope.commitment.chain_reader import read_commitment_of
    from hope.commitment.registration import (
        REG_V1_PREFIX,
        parse_registration_payload,
        verify_registration,
    )
    try:
        import bittensor as bt  # type: ignore
    except ImportError:
        print("ERROR: bittensor not installed", file=sys.stderr)
        return 2

    st = bt.Subtensor(network=args.network)
    fields = read_commitment_of(st, args.netuid, args.hotkey_ss58)
    if fields is None:
        print(f"no commitment present at (netuid={args.netuid}, "
              f"hotkey={args.hotkey_ss58[:16]}...)")
        return 5
    if not fields:
        print("commitment present but has no fields (unexpected)")
        return 5

    # Find a field whose bytes start with the registration prefix.
    payload: bytes | None = None
    matched_variant: str | None = None
    for f in fields:
        if f.bytes_.startswith(REG_V1_PREFIX):
            payload = f.bytes_
            matched_variant = f.variant
            break

    if payload is None:
        print(f"latest commitment has {len(fields)} field(s) but none look like a "
              f"registration payload (no '{REG_V1_PREFIX.decode()}' prefix):")
        for f in fields:
            print(f"  variant={f.variant} bytes={f.bytes_[:32].hex()}...")
        return 6

    parsed = parse_registration_payload(payload)
    if parsed is None:
        print(f"payload from variant {matched_variant} is NOT a valid registration"
              " (wrong length after prefix)")
        return 6
    ss58_pubkey = _ss58_to_raw_pubkey(args.hotkey_ss58)
    role = parsed.role
    sig_ok = verify_registration(parsed, ss58_pubkey=ss58_pubkey, expected_role=role)
    print(f"variant:     {matched_variant}")
    print(f"role:        {role.value} ({role.name})")
    print(f"ed25519 pk:  {parsed.ed25519_pk.hex()}")
    print(f"sig valid:   {sig_ok}")
    return 0 if sig_ok else 7


# ============================================================================
# CLI plumbing
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="SN21 ed25519 key-management CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="create a new ed25519 PEM")
    p_gen.add_argument("--role", choices=list(_ROLE_VALUES), required=True)
    p_gen.add_argument("--output", required=True, help="path to write PEM (will set mode 0600)")
    p_gen.add_argument("--force", action="store_true", help="overwrite if file exists")
    p_gen.set_defaults(func=cmd_generate)

    p_show = sub.add_parser("show", help="print the public key of a stored PEM")
    p_show.add_argument("--key", required=True)
    p_show.set_defaults(func=cmd_show)

    p_test = sub.add_parser("sign-test", help="round-trip sign+verify on a test message")
    p_test.add_argument("--key", required=True)
    p_test.set_defaults(func=cmd_sign_test)

    p_reg = sub.add_parser("register", help="submit the on-chain registration commit")
    p_reg.add_argument("--role", choices=list(_ROLE_VALUES), required=True)
    p_reg.add_argument("--netuid", type=int, required=True)
    p_reg.add_argument("--network", default="finney")
    p_reg.add_argument("--wallet-name", required=True)
    p_reg.add_argument("--wallet-hotkey", default="default")
    p_reg.add_argument("--key", required=True, help="path to ed25519 PEM")
    p_reg.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p_reg.set_defaults(func=cmd_register)

    p_ver = sub.add_parser("verify-reg",
                            help="read + verify the on-chain registration for a hotkey")
    p_ver.add_argument("--netuid", type=int, required=True)
    p_ver.add_argument("--network", default="finney")
    p_ver.add_argument("--hotkey-ss58", required=True)
    p_ver.set_defaults(func=cmd_verify_reg)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
