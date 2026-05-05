"""Operator's ed25519 public key for verifying API response signatures.

Every response from the data API is signed with the operator's private key.
The validator verifies the signature against this public key to prove
the data is authentic and unmodified.

If the operator rotates their signing key, this file must be updated and a
new release published. Validators should reject responses signed with
unknown keys.
"""

# Operator's ed25519 public key (hex-encoded, 32 bytes)
HOPE_PUBLIC_KEY_HEX = "fec083caae22e4c64b6aa3f5bba38bbd3b8afe522079cbc696d48b439138e2d3"
