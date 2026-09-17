#!/bin/sh
# Build only. Installation remains a separately reviewed root operation bound
# to a signed accepted-capsule manifest. This script refuses to replace output.
set -eu
umask 077

if [ "$#" -ne 1 ]; then
  echo "usage: $0 ABSOLUTE_NEW_OUTPUT" >&2
  exit 64
fi

output=$1
case "$output" in
  /*) ;;
  *) echo "output must be absolute" >&2; exit 64 ;;
esac
if [ -e "$output" ] || [ -L "$output" ]; then
  echo "refusing to replace existing launcher output" >&2
  exit 73
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
source_file="$script_dir/public-edge-capsule-launcher.c"
exec /usr/bin/cc \
  -std=c11 -O2 -pipe -static-pie \
  -fPIE -fstack-protector-strong -D_FORTIFY_SOURCE=3 \
  -Werror -Wall -Wextra -Wformat=2 \
  -Wl,-z,relro,-z,now,-z,noexecstack \
  -o "$output" "$source_file"
