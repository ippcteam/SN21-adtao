#!/usr/bin/env bash
# Focused probe: WHY does os.unshare() fail where the `unshare` binary works,
# and what can the binary actually do here? Ubuntu 24.04 gates unprivileged
# user-namespace creation behind AppArmor per-executable profiles; the fix for
# the sandbox depends on exactly which paths the binary permits.
set +e
echo "===UNSHARE-PROBE-START==="

echo "--- os-level userns restriction knobs ---"
sysctl kernel.apparmor_restrict_unprivileged_userns 2>&1 | head -1
sysctl kernel.unprivileged_userns_clone 2>&1 | head -1
cat /proc/sys/user/max_user_namespaces 2>&1

echo "--- (A) raw syscall from python3 (expected to EPERM under AppArmor) ---"
python3 - <<'PY' 2>&1
import os, ctypes, errno
try:
    os.unshare(os.CLONE_NEWUSER)
    print("py os.unshare(NEWUSER): OK")
except OSError as e:
    print(f"py os.unshare(NEWUSER): FAIL errno={e.errno} ({os.strerror(e.errno)})")
PY

echo "--- (B) the unshare BINARY, plain userns ---"
unshare --user --map-root-user -- id 2>&1 | head -2

echo "--- (C) unshare binary: user+net (the sandbox network wall) ---"
unshare --user --map-root-user --net -- sh -c 'echo NET_OK; ip link 2>/dev/null | head -3' 2>&1 | head -4

echo "--- (D) unshare binary: user+net+pid+fork ---"
unshare --user --map-root-user --net --pid --fork -- sh -c 'echo PIDNS_OK $$' 2>&1 | head -2

echo "--- (E) does unshare support --root (chroot) ? ---"
unshare --help 2>&1 | grep -iE '\--root|\--wd|--map-root' | head -5

echo "--- (F) unshare --root into a tiny rootfs ---"
RT=$(mktemp -d)
mkdir -p "$RT/bin"
cp /bin/busybox "$RT/bin/" 2>/dev/null || cp /bin/echo "$RT/bin/echo" 2>/dev/null
# copy libs echo needs, minimal
for l in /lib/x86_64-linux-gnu/libc.so.6 /lib64/ld-linux-x86-64.so.2; do
  [ -f "$l" ] && { mkdir -p "$RT$(dirname $l)"; cp "$l" "$RT$l"; }
done
unshare --user --map-root-user --net --root="$RT" --wd=/ -- /bin/echo ROOT_OK 2>&1 | head -2
rm -rf "$RT"

echo "--- (G) which util-linux ---"
unshare --version 2>&1 | head -1

echo "===UNSHARE-PROBE-END==="
sleep 100000
