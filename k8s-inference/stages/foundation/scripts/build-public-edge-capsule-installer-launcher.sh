#!/bin/sh
# Build only. The independently accepted binary is installed later by the
# security-owned package boundary. Existing outputs are never replaced.
set -eu
umask 077

if [ "$#" -ne 4 ]; then
  echo "usage: $0 ABSOLUTE_NEW_OUTPUT INSTALLER_SOURCE_SHA256 ABSOLUTE_STATIC_FROZEN_PYTHON STATIC_FROZEN_PYTHON_SHA256" >&2
  exit 64
fi
output=$1
source_sha256=$2
python_path=$3
python_sha256=$4
case "$output" in /*) ;; *) echo "output must be absolute" >&2; exit 64 ;; esac
case "$python_path" in /*) ;; *) echo "frozen Python path must be absolute" >&2; exit 64 ;; esac
if [ -e "$output" ] || [ -L "$output" ]; then
  echo "refusing to replace existing installer gate output" >&2
  exit 73
fi
for value in "$source_sha256" "$python_sha256"; do
  case "$value" in *[!0-9a-f]*|'') echo "accepted inputs must be lowercase SHA-256 digests" >&2; exit 64 ;; esac
  if [ "${#value}" -ne 64 ]; then
    echo "accepted inputs must be lowercase SHA-256 digests" >&2
    exit 64
  fi
done
/usr/local/libexec/fs2-verify-public-edge-frozen-runtime \
  --python "$python_path" \
  --expected-python-sha256 "$python_sha256"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec /usr/bin/cc \
  -std=c11 -O2 -pipe -static-pie \
  -fPIE -fstack-protector-strong -D_FORTIFY_SOURCE=3 \
  -DFS2_EXPECTED_INSTALLER_SOURCE_SHA256=\"$source_sha256\" \
  -DFS2_EXPECTED_INSTALLER_PYTHON_SHA256=\"$python_sha256\" \
  -Werror -Wall -Wextra -Wformat=2 \
  -Wl,-z,relro,-z,now,-z,noexecstack,--wrap=dlopen,--wrap=dlmopen \
  -o "$output" "$script_dir/public-edge-capsule-installer-launcher.c" -lcrypto -ldl -pthread
