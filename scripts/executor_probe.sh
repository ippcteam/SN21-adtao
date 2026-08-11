#!/usr/bin/env bash
# Capability probe for the model-executor host.
#
# The published sandbox contract (MINER_MODEL_SPEC §2) was written for a
# docker host: --network=none, memory cap, read-only rootfs, pids limit.
# Running the executor on a PaaS means those exact flags are unavailable, so
# before designing a replacement sandbox we measure what THIS environment
# actually permits: user namespaces, seccomp state, capabilities, tooling,
# disk. The output of this script is the factual basis for that design —
# guessing wrong here means either "cannot isolate miners' code" (unsafe) or
# "over-claimed and nothing runs" (dead in a different way).
#
# Prints everything between two markers, then idles so the worker does not
# restart-loop.
set +e

echo "===EXECUTOR-PROBE-START==="

echo "--- identity / kernel ---"
id
uname -a

echo "--- capabilities (of this process) ---"
grep -E "^Cap(Inh|Prm|Eff|Bnd)" /proc/self/status

echo "--- seccomp / no-new-privs ---"
grep -E "^(Seccomp|NoNewPrivs)" /proc/self/status

echo "--- user namespaces ---"
cat /proc/sys/user/max_user_namespaces 2>&1

echo "--- unshare tests (what isolation we can create) ---"
unshare -U true 2>&1 && echo "unshare -U: OK" || echo "unshare -U: FAIL"
unshare -n true 2>&1 && echo "unshare -n: OK" || echo "unshare -n: FAIL"
unshare -Urn true 2>&1 && echo "unshare -Urn: OK" || echo "unshare -Urn: FAIL"
unshare -Urm true 2>&1 && echo "unshare -Urm: OK" || echo "unshare -Urm: FAIL"

echo "--- container tooling present ---"
for b in docker podman runc crun bwrap proot skopeo umoci unshare nsenter \
         fusermount fuse-overlayfs newuidmap newgidmap; do
  printf "%s=" "$b"
  command -v "$b" >/dev/null 2>&1 && echo yes || echo no
done

echo "--- devices / sockets ---"
ls -l /dev/fuse 2>&1 | head -1
ls -l /var/run/docker.sock 2>&1 | head -1

echo "--- subuid/subgid (rootless containers need these) ---"
cat /etc/subuid 2>&1 | head -3
cat /etc/subgid 2>&1 | head -3

echo "--- cgroups (for memory/pids limits) ---"
cat /sys/fs/cgroup/cgroup.controllers 2>&1 | head -1
ls /sys/fs/cgroup/ 2>&1 | head -5

echo "--- filesystem / disk ---"
df -h / /tmp 2>&1 | tail -3
mount | grep -E " / | /tmp " 2>&1 | head -4

echo "--- network egress (registries reachable?) ---"
for host in ghcr.io registry-1.docker.io; do
  printf "%s: " "$host"
  curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://$host/v2/" 2>&1
done

echo "--- python ---"
python3 --version 2>&1
pip --version 2>&1 | head -1

echo "===EXECUTOR-PROBE-END==="

# Idle so the worker doesn't restart-loop while we read the output.
sleep 1000000
