#!/bin/sh
# Build only. Installation and policy enrollment remain a distinct privileged,
# independently reviewed append-only operation.
set -eu
umask 077

if [ "$#" -ne 3 ]; then
  echo "usage: $0 ABSOLUTE_NEW_OUTPUT VERIFIER_SOURCE_SHA256 FROZEN_VERIFIER_PYTHON_SHA256" >&2
  exit 64
fi
output=$1
verifier_sha256=$2
python_sha256=$3
case "$output" in /*) ;; *) echo "output must be absolute" >&2; exit 64 ;; esac
if [ -e "$output" ] || [ -L "$output" ]; then
  echo "refusing to replace existing verifier launcher output" >&2
  exit 73
fi
for value in "$verifier_sha256" "$python_sha256"; do
  case "$value" in *[!0-9a-f]*|'') echo "accepted inputs must be lowercase SHA-256 digests" >&2; exit 64 ;; esac
  if [ "${#value}" -ne 64 ]; then
    echo "accepted inputs must be lowercase SHA-256 digests" >&2
    exit 64
  fi
done
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec /usr/bin/cc \
  -std=c11 -O2 -pipe -static -fno-pie -no-pie -fstack-protector-strong -D_FORTIFY_SOURCE=3 \
  -DFS2_EXPECTED_VERIFIER_SHA256=\"$verifier_sha256\" \
  -DFS2_EXPECTED_VERIFIER_PYTHON_SHA256=\"$python_sha256\" \
  -Werror -Wall -Wextra -Wformat=2 \
  -Wl,-z,relro,-z,now,-z,noexecstack,--wrap=dlopen,--wrap=dlmopen \
  -o "$output" "$script_dir/public-edge-frozen-runtime-verifier-launcher.c" \
  -lcrypto -ldl -pthread
