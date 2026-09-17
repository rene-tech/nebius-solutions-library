#!/bin/sh
# Build only. Installation remains a separately reviewed root operation bound
# to a signed accepted-capsule manifest. This script refuses to replace output.
set -eu
umask 077

if [ "$#" -ne 4 ]; then
  echo "usage: $0 ABSOLUTE_NEW_OUTPUT BOOTSTRAP_SHA256 STATIC_FROZEN_PYTHON_SHA256 FROZEN_RUNTIME_CLOSURE_REVIEW_SHA256" >&2
  exit 64
fi

output=$1
bootstrap_sha256=$2
python_sha256=$3
frozen_runtime_review_sha256=$4
case "$output" in
  /*) ;;
  *) echo "output must be absolute" >&2; exit 64 ;;
esac
if [ -e "$output" ] || [ -L "$output" ]; then
  echo "refusing to replace existing launcher output" >&2
  exit 73
fi
for value in "$bootstrap_sha256" "$python_sha256" "$frozen_runtime_review_sha256"; do
  case "$value" in *[!0-9a-f]*|'') echo "accepted inputs must be lowercase SHA-256 digests" >&2; exit 64 ;; esac
  if [ "${#value}" -ne 64 ]; then
    echo "accepted inputs must be lowercase SHA-256 digests" >&2
    exit 64
  fi
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
source_file="$script_dir/public-edge-capsule-launcher.c"
exec /usr/bin/cc \
  -std=c11 -O2 -pipe -static-pie \
  -fPIE -fstack-protector-strong -D_FORTIFY_SOURCE=3 \
  -DFS2_EXPECTED_BOOTSTRAP_SHA256=\"$bootstrap_sha256\" \
  -DFS2_EXPECTED_PYTHON_SHA256=\"$python_sha256\" \
  -DFS2_EXPECTED_FROZEN_RUNTIME_REVIEW_SHA256=\"$frozen_runtime_review_sha256\" \
  -Werror -Wall -Wextra -Wformat=2 \
  -Wl,-z,relro,-z,now,-z,noexecstack \
  -o "$output" "$source_file"
