#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 ABSOLUTE_NEW_OUTPUT_DIRECTORY GO_BUILDER_IMAGE_AT_SHA256" >&2
    exit 2
fi

output_directory=$1
builder_image=$2

case "$output_directory" in
    /*) ;;
    *) echo "output directory must be absolute" >&2; exit 2 ;;
esac

builder_digest=${builder_image##*@sha256:}
case "$builder_image" in
    *@sha256:*) ;;
    *) echo "builder image must be pinned by a sha256 digest" >&2; exit 2 ;;
esac
case "$builder_image" in
    *[!A-Za-z0-9._:/@-]*) echo "builder image contains unsupported characters" >&2; exit 2 ;;
esac
if [ "${#builder_digest}" -ne 64 ]; then
    echo "builder image digest must contain exactly 64 lowercase hex characters" >&2
    exit 2
fi
case "$builder_digest" in
    *[!0-9a-f]*) echo "builder image digest must be canonical lowercase hex" >&2; exit 2 ;;
esac

if [ -e "$output_directory" ]; then
    echo "output directory already exists; retained output is never overwritten" >&2
    exit 1
fi

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
component_directory=$(CDPATH= cd -- "$script_directory/.." && pwd)

umask 077
mkdir -m 0700 -- "$output_directory"
docker buildx build \
    --build-arg "FS2_GO_BUILDER_IMAGE=$builder_image" \
    --target enrollment-tools-output \
    --output "type=local,dest=$output_directory" \
    "$component_directory"

for artifact in public-edge-enrollment-compiler public-edge-enrollment-assembler public-edge-snapshot-authority; do
    if [ ! -f "$output_directory/$artifact" ] || [ -L "$output_directory/$artifact" ]; then
        echo "build output is missing exact regular artifact $artifact" >&2
        exit 1
    fi
    chmod 0500 -- "$output_directory/$artifact"
done

compiler_sha256=$(sha256sum -- "$output_directory/public-edge-enrollment-compiler" | awk '{print $1}')
assembler_sha256=$(sha256sum -- "$output_directory/public-edge-enrollment-assembler" | awk '{print $1}')
snapshot_authority_sha256=$(sha256sum -- "$output_directory/public-edge-snapshot-authority" | awk '{print $1}')

set -C
printf '%s' "{\"schema\":\"fs2-serve.nebius.ai/public-edge-enrollment-tools/v1\",\"builder_image\":\"$builder_image\",\"artifacts\":[{\"name\":\"public-edge-enrollment-assembler\",\"sha256\":\"$assembler_sha256\"},{\"name\":\"public-edge-enrollment-compiler\",\"sha256\":\"$compiler_sha256\"},{\"name\":\"public-edge-snapshot-authority\",\"sha256\":\"$snapshot_authority_sha256\"}]}" > "$output_directory/enrollment-tools-manifest.json"
chmod 0400 -- "$output_directory/enrollment-tools-manifest.json"
