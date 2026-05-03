"""Layer 9.E — HOPE Shadow Validator.

Independent validator that runs the same scoring algorithm and posts shadow
9.C.1/9.C.2/9.C.3/(9.C.6) commits to its own hotkey. SH-5 rubber-stamp
defence is structural via inner_sig over plaintext.validator_hotkey.
"""

from hope.hope_shadow_validator.runner import run_shadow_epoch

__all__ = ["run_shadow_epoch"]
