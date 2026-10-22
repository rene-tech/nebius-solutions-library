#!/usr/bin/env bash
# charts/platform/files/node-config.sh (the containerd registry configuration of a node, docs/IMAGES.md): the script
# OWNS every per-registry mirror file (the logical host and the public registries): peer NodePort first, then the
# cache, valid TOML with unique tables, replaced atomically and only when different (idempotent; a second run writes
# nothing); a fresh node also gets the memlock drop-in and ONE containerd restart; a node that already serves the
# mirrors with unlimited memlock sees no restart however its mirror files change (a hosts-file change alone never
# restarts containerd). Host commands are stubs; nothing leaves the temp directory. Run by tools/check.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/charts/platform/files/node-config.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
B="$T/bin"; mkdir -p "$B"
cat > "$B/nsenter" <<'EOF'
#!/bin/bash
# the host's systemd/pidof through nsenter: run the part after `--` against the stubs (which record themselves)
while [ "$1" != "--" ]; do shift; done; shift
exec "$@"
EOF
cat > "$B/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"; exit 0
EOF
cat > "$B/pidof" <<'EOF'
#!/bin/bash
echo 4242
EOF
cat > "$B/chroot" <<'EOF'
#!/bin/bash
# chroot <root> <cmd...>: run the command against the stubs
shift; exec "$@"
EOF
cat > "$B/crictl" <<'EOF'
#!/bin/bash
echo "crictl $*" >> "$CALLS"; [ "${PULL_OK:-1}" = 1 ]
EOF
cat > "$B/containerd" <<'EOF'
#!/bin/bash
echo "containerd $*" >> "$CALLS"; exit 0
EOF
chmod +x "$B"/*
node() { # <dir> <memlock 0|1> <hosts none|spegel|boot|dup> <limit bounded|unlimited>: a host filesystem
  local d=$1 memlock=$2 hosts=$3 limit=$4
  local h="$d/etc/containerd/certs.d/registry.serverless2.local"
  mkdir -p "$d/etc/containerd/certs.d" "$d/etc/systemd/system/containerd.service.d" "$d/proc/4242"
  printf 'version = 2\n[plugins."io.containerd.grpc.v1.cri".containerd]\n  snapshotter = "overlayfs"\n' > "$d/etc/containerd/config.toml"
  [ "$memlock" = 1 ] && printf '[Service]\nLimitMEMLOCK=infinity\n' > "$d/etc/systemd/system/containerd.service.d/serverless2-memlock.conf"
  if [ "$limit" = unlimited ]; then printf 'Max locked memory         unlimited            unlimited            bytes\n' > "$d/proc/4242/limits"; else printf 'Max locked memory         8388608              8388608              bytes\n' > "$d/proc/4242/limits"; fi
  case "$hosts" in
    spegel) mkdir -p "$h"; printf "server = 'https://registry.serverless2.local'\n\n[host.'http://10.0.0.7:30020']\ncapabilities = ['pull', 'resolve']\ndial_timeout = '200ms'\n\n[host.'http://10.0.0.7:30021']\ncapabilities = ['pull', 'resolve']\ndial_timeout = '200ms'\n\n[host.'http://127.0.0.1:30500']\ncapabilities = ['pull', 'resolve']\ndial_timeout = '200ms'\n" > "$h/hosts.toml";;
    boot) mkdir -p "$h"; printf 'server = "http://127.0.0.1:30500"\n\n[host."http://127.0.0.1:30500"]\n  capabilities = ["pull", "resolve"]\n' > "$h/hosts.toml";;
    dup) mkdir -p "$h"; printf "server = 'https://registry.serverless2.local'\n\n[host.'http://10.0.0.7:30021']\ncapabilities = ['pull', 'resolve']\n\n[host.'http://10.0.0.7:30021']\ncapabilities = ['pull', 'resolve']\n" > "$h/hosts.toml";;
  esac
  return 0
}
run() { # <dir> <pull ok> -> stdout; CALLS reset
  CALLS="$T/calls.txt"; : > "$CALLS"
  PATH="$B:$PATH" CALLS="$CALLS" PULL_OK="$2" HOST_ROOT="$1" IMAGES_HOST=registry.serverless2.local CACHE_NODE_PORT=30500 NODE_IP=10.0.0.7 PEER_NODE_PORT=30021 \
    MIRRORS="docker.io=https://registry-1.docker.io ghcr.io=https://ghcr.io" SETTLE_S=0 sh "$S"
}
valid() { # <hosts.toml> <server>: parses as TOML, exactly the two hosts in order (peer first, then the cache), the server
  python3 - "$1" "$2" <<'PY'
import sys, tomllib
f, server = sys.argv[1], sys.argv[2]
raw = open(f, "rb").read()
d = tomllib.loads(raw.decode())  # raises on a duplicate table
hosts = list(d.get("host", {}))
assert d["server"] == server, (f, d["server"])
assert hosts == ["http://10.0.0.7:30021", "http://127.0.0.1:30500"], (f, hosts)
assert all(d["host"][h]["capabilities"] == ["pull", "resolve"] and d["host"][h]["dial_timeout"] == "200ms" for h in hosts)
PY
}
files() { echo "$1/etc/containerd/certs.d/registry.serverless2.local/hosts.toml https://registry.serverless2.local $1/etc/containerd/certs.d/docker.io/hosts.toml https://registry-1.docker.io $1/etc/containerd/certs.d/ghcr.io/hosts.toml https://ghcr.io"; }
check_files() { set -- $(files "$1"); while [ $# -gt 0 ]; do [ -f "$1" ] || fail "missing mirror file $1"; valid "$1" "$2" || fail "invalid mirror file $1"; shift 2; done; }

# 1. a fresh CPU node (nothing written yet, memlock bounded, mirrors not served): the three mirror files, memlock + ONE restart
node "$T/fresh" 0 none bounded
out=$(run "$T/fresh" 0) || fail "fresh node: script failed: $out"
check_files "$T/fresh"
[ -f "$T/fresh/etc/systemd/system/containerd.service.d/serverless2-memlock.conf" ] || fail "fresh node: no memlock drop-in"
[ -f "$T/fresh/etc/containerd/conf.d/serverless2-registry.toml" ] || fail "fresh node: no containerd drop-in"
grep -q '^imports = \["/etc/containerd/conf.d/\*.toml"\]' "$T/fresh/etc/containerd/config.toml" || fail "fresh node: imports line not added"
[ "$(grep -c 'systemctl restart containerd' "$T/calls.txt")" = 1 ] || fail "fresh node: expected exactly one containerd restart, got: $(cat "$T/calls.txt")"
grep -q 'systemctl daemon-reload' "$T/calls.txt" || fail "fresh node: no daemon-reload for the memlock drop-in"
[ -f "$T/fresh/etc/containerd/certs.d/.serverless2-containerd-restarted" ] || fail "fresh node: no restart marker"
[ "$(echo "$out" | grep -c 'mirror file for .* written')" = 3 ] || fail "fresh node: expected three written files: $out"
echo "   ok fresh node: three valid mirror files (peer first, then the cache), memlock drop-in, drop-in, one restart, marker"

# 2. idempotence: the same node again (its restarted containerd now has unlimited memlock): no file changes, no systemd call
printf 'Max locked memory         unlimited            unlimited            bytes\n' > "$T/fresh/proc/4242/limits"
shas=$(cd "$T/fresh" && find etc/containerd/certs.d -name hosts.toml -exec sha256sum {} + | sort)
out=$(run "$T/fresh" 1) || fail "second run: script failed: $out"
[ "$(cd "$T/fresh" && find etc/containerd/certs.d -name hosts.toml -exec sha256sum {} + | sort)" = "$shas" ] || fail "second run: a mirror file changed"
[ "$(echo "$out" | grep -c 'mirror file for .*: unchanged')" = 3 ] || fail "second run: not reported unchanged: $out"
grep -q 'systemctl' "$T/calls.txt" && fail "second run: systemd was called: $(cat "$T/calls.txt")"
for leftover in "$T"/fresh/etc/containerd/certs.d/*/hosts.toml.node-config.*; do [ -e "$leftover" ] && fail "a temporary was left behind: $leftover"; done
echo "   ok idempotent: second run writes nothing, no restart, no temporaries"

# 3. a served node with Spegel's old 3-entry file (dead hostPort first), memlock unlimited: the file is REPLACED by the
#    owned one, its original kept once, no drop-in, no restart (a hosts-file change alone never restarts containerd)
node "$T/served" 1 spegel unlimited
cfg_before=$(sha256sum "$T/served/etc/containerd/config.toml")
out=$(run "$T/served" 1) || fail "served node: script failed: $out"
check_files "$T/served"
H="$T/served/etc/containerd/certs.d/registry.serverless2.local/hosts.toml"
[ -f "$H.pre-node-config" ] && grep -q '30020' "$H.pre-node-config" || fail "served node: the replaced file was not kept"
[ "$(sha256sum "$T/served/etc/containerd/config.toml")" = "$cfg_before" ] || fail "served node: config.toml was touched"
[ ! -f "$T/served/etc/containerd/conf.d/serverless2-registry.toml" ] || fail "served node: a drop-in was written although the runtime serves the mirrors"
grep -q 'systemctl' "$T/calls.txt" && fail "served node: systemd was called for a hosts-file change: $(cat "$T/calls.txt")"
[ -f "$T/served/etc/containerd/certs.d/.serverless2-containerd-restarted" ] || fail "served node: the marker must be set once the probe passes"
pre=$(sha256sum "$H.pre-node-config"); run "$T/served" 1 > /dev/null; [ "$(sha256sum "$H.pre-node-config")" = "$pre" ] || fail "served node: the kept original was overwritten on a later run"
echo "   ok served node: old Spegel file replaced (original kept once), no drop-in, no restart"

# 4. a served runtime whose memlock drop-in is missing: drop-in + one restart; the mirror files are owned as well
node "$T/stale" 1 spegel unlimited
out=$(run "$T/stale" 0) || fail "stale runtime: script failed: $out"
check_files "$T/stale"
[ "$(grep -c 'systemctl restart containerd' "$T/calls.txt")" = 1 ] || fail "stale runtime: expected one restart"
grep -q 'systemctl daemon-reload' "$T/calls.txt" && fail "stale runtime: daemon-reload without a memlock change"
echo "   ok stale runtime: one restart with the drop-in, mirror files owned"

# 5. reboot: a node booted with the cloud-init fragment (bootstrap file: the cache only, memlock present, mirrors served):
#    the bootstrap file is completed to the owned one, nothing else happens
node "$T/booted" 1 boot unlimited
out=$(run "$T/booted" 1) || fail "booted node: script failed: $out"
check_files "$T/booted"
grep -q 'systemctl' "$T/calls.txt" && fail "booted node: systemd was called"
echo "   ok booted node (cloud-init fragment): bootstrap file completed, no restart"

# 6. a configured node (marker from an earlier run, memlock unlimited) whose probe pull FAILS right after the hosts-file
#    rewrite (a transient, or the mirrors themselves): retried, reported, NEVER a containerd restart; the files are owned
node "$T/transient" 1 spegel unlimited
touch "$T/transient/etc/containerd/certs.d/.serverless2-containerd-restarted"
out=$(PROBE_RETRIES=2 run "$T/transient" 0) || fail "transient: script failed: $out"
check_files "$T/transient"
grep -q 'systemctl' "$T/calls.txt" && fail "transient: systemd was called on a configured node: $(cat "$T/calls.txt")"
[ "$(grep -c 'crictl pull' "$T/calls.txt")" = 3 ] || fail "transient: expected the probe and two retries, got: $(grep -c 'crictl pull' "$T/calls.txt")"
echo "$out" | grep -q 'still fails after 2 retries' || fail "transient: not reported: $out"
[ ! -f "$T/transient/etc/containerd/conf.d/serverless2-registry.toml" ] || fail "transient: a drop-in was written"
echo "   ok configured node + failing probe: retried, reported, no restart, files owned"

# 7. a broken file (the same [host] table twice, which containerd rejects): replaced by a valid one, no restart
node "$T/dup" 1 dup unlimited
python3 -c 'import sys,tomllib; tomllib.loads(open(sys.argv[1]).read())' "$T/dup/etc/containerd/certs.d/registry.serverless2.local/hosts.toml" 2>/dev/null && fail "dup fixture is not a duplicate-table file"
out=$(run "$T/dup" 1) || fail "dup: script failed: $out"
check_files "$T/dup"
grep -q 'systemctl' "$T/calls.txt" && fail "dup: systemd was called"
echo "   ok duplicate-table file: replaced by a valid one, no restart"
echo "node-config: all checks passed"
