#!/bin/sh
# node-config (charts/platform/templates/node-config, docs/IMAGES.md "The setting at boot"): run once per node by the
# DaemonSet's init container against the host's filesystem (HOST_ROOT, /host) with the host's systemd through nsenter.
# What it guarantees on a node, in this order, touching nothing that is already right:
#   1. the per-registry mirror files (hosts.toml for the logical host and for every public registry Spegel mirrors):
#      this script is their SOLE owner (Spegel's own writer is off, clusters/common/values/spegel.yaml): one peer entry
#      first (the Spegel registry NodePort on this node's address), then the cache on the loopback; written atomically
#      and only when the file differs (idempotent), replacing whatever is there (cloud-init's bootstrap, an old
#      Spegel-written file, a broken one). A hosts-file change alone never restarts containerd: it re-reads certs.d
#      per pull;
#   2. the unlimited memlock on the containerd unit (a drop-in; a daemon-reload and one restart when it was missing);
#   3. containerd serving the per-registry mirror files: decided on the RUNNING process by a probe pull through the
#      logical host, never on a file; only when that fails is the drop-in written and containerd restarted once.
# A node that already serves the mirrors with unlimited memlock sees no write and no restart (the common case: every
# GPU node and every CPU batch node booted with the cloud-init fragment; every node this ran on before).
set -e
HOST_ROOT=${HOST_ROOT:-/host}
: "${IMAGES_HOST:?IMAGES_HOST is required}"
CACHE_NODE_PORT=${CACHE_NODE_PORT:-30500}
CFG=$HOST_ROOT/etc/containerd/config.toml
DROPIN=$HOST_ROOT/etc/containerd/conf.d/serverless2-registry.toml
CERTS=$HOST_ROOT/etc/containerd/certs.d
MARK=$CERTS/.serverless2-containerd-restarted
MEMLOCK=$HOST_ROOT/etc/systemd/system/containerd.service.d/serverless2-memlock.conf
PROBE=$IMAGES_HOST/docker/library/busybox:1.36
host_systemctl() { nsenter -t 1 -m -u -i -n -p -- systemctl "$@"; }
host_pid() { nsenter -t 1 -m -u -i -n -p -- pidof containerd | cut -d' ' -f1; }
probe_pull() { chroot "$HOST_ROOT" crictl pull "$PROBE" > /dev/null 2>&1; }

mkdir -p "$CERTS"
# 1. the mirror files, owned here. NODE_IP: the node's address (the DaemonSet's status.hostIP); PEER_NODE_PORT: the
# Spegel registry NodePort; MIRRORS: "<dir>=<upstream url> ..." for the public registries (the logical host always).
: "${NODE_IP:?NODE_IP is required}"
PEER_NODE_PORT=${PEER_NODE_PORT:-30021}
write_mirror() { # <dir> <server url>: the file this node must have; replaced only when it differs (atomic rename)
  dir="$CERTS/$1"; f="$dir/hosts.toml"; tmp="$f.node-config.$$"
  mkdir -p "$dir"
  printf "server = '%s'\n\n[host.'http://%s:%s']\ncapabilities = ['pull', 'resolve']\ndial_timeout = '200ms'\n\n[host.'http://127.0.0.1:%s']\ncapabilities = ['pull', 'resolve']\ndial_timeout = '200ms'\n" "$2" "$NODE_IP" "$PEER_NODE_PORT" "$CACHE_NODE_PORT" > "$tmp"
  if [ -f "$f" ] && cmp -s "$tmp" "$f"; then rm -f "$tmp"; echo "mirror file for $1: unchanged"; return 0; fi
  if [ -f "$f" ] && [ ! -f "$f.pre-node-config" ]; then cp "$f" "$f.pre-node-config"; fi   # the first replaced file, kept once
  mv "$tmp" "$f" && echo "mirror file for $1 written (peer $NODE_IP:$PEER_NODE_PORT first, then the cache on 127.0.0.1:$CACHE_NODE_PORT)"
}
write_mirror "$IMAGES_HOST" "https://$IMAGES_HOST"
# shellcheck disable=SC2086  # MIRRORS is a space-separated list of <dir>=<url> by contract (the DaemonSet renders it)
for m in ${MIRRORS:-}; do write_mirror "${m%%=*}" "${m#*=}"; done
# the exact host tables now on this node, one line per file: the rollout readback reads these from the pod log of
# every node (kubectl logs -c configure), no debug pod needed
for f in "$CERTS"/*/hosts.toml; do echo "mirror tables $(basename "$(dirname "$f")"): $(grep -o "^\[host\.'[^']*'\]" "$f" | tr '\n' ' ')"; done
# 2. memlock (docs/JOBS.md "Multi-node runs"): RDMA memory registration is bounded by RLIMIT_MEMLOCK (8 MiB on the node
# image); an unlimited limit on the containerd unit lets every container register memory without CAP_IPC_LOCK
NEED_RESTART=0
if [ ! -f "$MEMLOCK" ]; then
  mkdir -p "$(dirname "$MEMLOCK")"
  printf '# serverless2 (docs/JOBS.md): RDMA pins memory; an unlimited memlock limit on the runtime lets every container\n# register memory without CAP_IPC_LOCK (the node image ships 8 MiB).\n[Service]\nLimitMEMLOCK=infinity\n' > "$MEMLOCK"
  host_systemctl daemon-reload
  NEED_RESTART=1
fi
cpid=$(host_pid || true)
if [ -n "$cpid" ] && ! grep -q 'locked memory.*unlimited' "$HOST_ROOT/proc/$cpid/limits" 2>/dev/null; then NEED_RESTART=1; fi
# 3. the running containerd serves the mirrors? (functional: a probe pull through the logical host, served by Spegel or
# the cache). Yes + memlock unlimited: nothing to do
if [ "$NEED_RESTART" = 0 ] && probe_pull; then
  [ -f "$MARK" ] || touch "$MARK"; echo "containerd serves the registry mirrors (probe pull ok) with unlimited memlock"; exit 0
fi
# A node that is already configured (the restart marker from an earlier run, memlock unlimited on the running process)
# needs no restart for a hosts-file change: containerd re-reads certs.d per pull, and a probe that fails here is
# transient (the cache or a peer busy, a pull in flight) or the mirror's own problem, never fixed by a restart. Retry
# briefly and report; a genuine repair (no marker, bounded memlock, no drop-in) keeps its one restart below.
if [ "$NEED_RESTART" = 0 ] && [ -f "$MARK" ]; then
  n=0; while [ $n -lt "${PROBE_RETRIES:-3}" ]; do n=$((n + 1)); sleep "${SETTLE_S:-5}"; probe_pull && { echo "containerd serves the registry mirrors (probe pull ok after $n retries) with unlimited memlock"; exit 0; }; done
  echo "!! configured node: the probe pull through $PROBE still fails after $n retries (the mirrors, not the runtime: no restart); the next rollout re-checks"; exit 0
fi
# Legacy repair: an earlier version appended the two tables to config.toml itself, which duplicates the cri.containerd
# table and makes the next containerd start fail. Drop that trailing block.
if [ "$(grep -c '^\[plugins."io.containerd.grpc.v1.cri".containerd\]$' "$CFG")" -gt 1 ]; then
  cp "$CFG" "$CFG.serverless2-backup"
  awk -v start="$(grep -n '^\[plugins."io.containerd.grpc.v1.cri".registry\]$' "$CFG" | tail -1 | cut -d: -f1)" 'NR < start' "$CFG" > "$CFG.new" && mv "$CFG.new" "$CFG"
  echo "removed the appended registry block from config.toml (backup: config.toml.serverless2-backup)"
fi
# a drop-in, never an edit of config.toml: its tables (cri.containerd) already exist there and TOML forbids defining a
# table twice. The imports line must precede the first table: after `version`.
mkdir -p "$HOST_ROOT/etc/containerd/conf.d"
grep -q '^imports' "$CFG" || sed -i '1a imports = ["/etc/containerd/conf.d/*.toml"]' "$CFG"
printf '# serverless2 image cache (docs/IMAGES.md): per-registry mirror configuration (Spegel peers, Zot cache)\nversion = 2\n[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"\n[plugins."io.containerd.grpc.v1.cri".containerd]\n  discard_unpacked_layers = false\n' > "$DROPIN"
chroot "$HOST_ROOT" containerd -c /etc/containerd/config.toml config dump > /dev/null   # parse check before a restart
host_systemctl restart containerd && touch "$MARK" && echo "containerd restarted with the registry configuration"
sleep "${SETTLE_S:-5}"; probe_pull && echo "verified: probe pull through the mirrors ok" || echo "configured; probe pull through $PROBE not yet possible (the cache or Spegel is not up yet: the next rollout re-checks)"
cpid=$(host_pid || true); [ -n "$cpid" ] && grep -q 'locked memory.*unlimited' "$HOST_ROOT/proc/$cpid/limits" 2>/dev/null && echo "verified: containerd memlock unlimited" || echo "!! containerd memlock limit still bounded (drop-in $MEMLOCK not applied?)"
