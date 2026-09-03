#!/usr/bin/env bash
set -euo pipefail

runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock_file="${runtime_dir}/image-lock.json"
mode=build
output_dir="${runtime_dir}/evidence/local"
registry_root="${FS2_REGISTRY_ROOT:-$(jq -r '.registry_default' "$lock_file")}"
adapter_worktree="${FS2_RUNTIME_ADAPTER_WORKTREE:-}"
declare -a requested=()

usage() {
  printf '%s\n' \
    'usage: build-and-publish.sh [--publish] --adapter-worktree DIR [--registry-root ROOT] [--output-dir DIR] [IMAGE_ID ...]' \
    '' \
    'Builds linux/amd64 images, runs the weight-free smoke test, and writes SBOMs.' \
    '--publish additionally refuses existing tags, pushes once, and records digests.'
}

while (($#)); do
  case "$1" in
    --publish)
      mode=publish
      shift
      ;;
    --output-dir)
      output_dir="${2:?--output-dir requires a value}"
      shift 2
      ;;
    --registry-root)
      registry_root="${2:?--registry-root requires a value}"
      shift 2
      ;;
    --adapter-worktree)
      adapter_worktree="${2:?--adapter-worktree requires a value}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      requested+=("$1")
      shift
      ;;
  esac
done

if [[ ! "$registry_root" =~ ^[a-z0-9.-]+/[a-z0-9][a-z0-9._/-]*[a-z0-9]$ ]]; then
  printf 'invalid registry root: %s\n' "$registry_root" >&2
  exit 2
fi
if [[ -z "$adapter_worktree" || ! -d "$adapter_worktree" ]]; then
  printf '%s\n' \
    'a concrete runtime adapter worktree is required via --adapter-worktree or FS2_RUNTIME_ADAPTER_WORKTREE' >&2
  exit 2
fi

command -v git >/dev/null || {
  printf 'required tool is missing: git\n' >&2
  exit 1
}
repo_root="$(git -C "$runtime_dir" rev-parse --show-toplevel)"
git -C "$repo_root" fetch --quiet --no-tags origin \
  refs/heads/main:refs/remotes/origin/main
task_head="$(git -C "$repo_root" rev-parse HEAD)"
main_head="$(git -C "$repo_root" rev-parse refs/remotes/origin/main)"
merge_base="$(git -C "$repo_root" merge-base HEAD refs/remotes/origin/main)"
read -r task_ahead task_behind < <(
  git -C "$repo_root" rev-list --left-right --count \
    HEAD...refs/remotes/origin/main
)
ahead_behind="${task_ahead}/${task_behind}"
if ! git -C "$repo_root" merge-base --is-ancestor \
  refs/remotes/origin/main HEAD; then
  printf '%s\n' \
    "refusing stale-base build: HEAD=${task_head} origin/main=${main_head} merge-base=${merge_base} ahead/behind=${ahead_behind}" \
    'integrate the fetched origin/main into the task branch and rerun every source/cross-contract gate first' >&2
  exit 1
fi
if [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all)" ]]; then
  printf '%s\n' \
    "refusing uncommitted-source build from HEAD=${task_head}" \
    'the reviewed task source must be clean and commit-addressable' >&2
  exit 1
fi
printf 'SOURCE_INTEGRATION head=%s origin_main=%s merge_base=%s ahead_behind=%s\n' \
  "$task_head" "$main_head" "$merge_base" "$ahead_behind"

"${runtime_dir}/check.sh"
python3 "${runtime_dir}/tests/verify_runtime_adapter_contract.py" \
  --adapter-worktree "$adapter_worktree"
adapter_revision="$(git -C "$adapter_worktree" rev-parse HEAD)"
adapter_branch="$(git -C "$adapter_worktree" symbolic-ref --short HEAD)"

for tool in docker crane jq syft; do
  command -v "$tool" >/dev/null || {
    printf 'required tool is missing: %s\n' "$tool" >&2
    exit 1
  }
done

mkdir -p "$output_dir"
receipt_lines="$(mktemp /tmp/fs2-structure-image-receipts.XXXXXX)"
trap 'rm -f "$receipt_lines"' EXIT

target_state() {
  local target="$1"
  local detail
  if detail="$(crane digest "$target" 2>&1)"; then
    printf 'exists:%s\n' "$detail"
    return 0
  fi
  case "$detail" in
    *'404 Not Found'*|*'MANIFEST_UNKNOWN'*|*'NAME_UNKNOWN'*)
      printf 'absent\n'
      return 10
      ;;
    *)
      printf 'registry inspection failed for %s: %s\n' "$target" "$detail" >&2
      return 1
      ;;
  esac
}

mapfile -t all_ids < <(jq -r '.images[].id' "$lock_file")
if ((${#requested[@]} == 0)); then
  requested=("${all_ids[@]}")
fi

for id in "${requested[@]}"; do
  if ! jq -e --arg id "$id" '.images[] | select(.id == $id)' "$lock_file" >/dev/null; then
    printf 'unknown image id: %s\n' "$id" >&2
    exit 2
  fi

  image="$(jq -c --arg id "$id" '.images[] | select(.id == $id)' "$lock_file")"
  dockerfile="$(jq -r '.dockerfile' <<<"$image")"
  repository="$(jq -r '.repository' <<<"$image")"
  tag="$(jq -r '.tag' <<<"$image")"
  target="${registry_root}/${repository}:${tag}"
  revision="$(jq -r '.source.revision' <<<"$image")"
  source_epoch="$(jq -r '.source.source_date_epoch' <<<"$image")"
  local_ref="fs2-structure-secondary/${id}:${tag}"

  printf 'SOURCE id=%s revision=%s\n' "$id" "$revision"
  printf 'TARGET %s\n' "$target"
  if [[ "$mode" == publish ]]; then
    state_rc=0
    state="$(target_state "$target")" || state_rc=$?
    if [[ "$state_rc" -eq 0 ]]; then
      printf 'refusing to overwrite existing target %s (%s)\n' "$target" "$state" >&2
      exit 1
    elif [[ "$state_rc" -ne 10 ]]; then
      exit "$state_rc"
    fi
    printf 'TARGET_STATE before_build=absent\n'
  fi

  build_args=()
  while IFS= read -r arg; do
    build_args+=(--build-arg "$arg")
  done < <(jq -r '.build_args | to_entries[] | "\(.key)=\(.value)"' <<<"$image")

  docker buildx build \
    --platform linux/amd64 \
    --progress plain \
    --pull \
    --load \
    --file "${runtime_dir}/${dockerfile}" \
    --label "org.opencontainers.image.created=$(date -u --date="@${source_epoch}" +%Y-%m-%dT%H:%M:%SZ)" \
    --label "org.opencontainers.image.revision=${revision}" \
    --tag "$local_ref" \
    "${build_args[@]}" \
    "$runtime_dir"

  smoke_rc=0
  smoke_output="$(docker run --rm --network none --platform linux/amd64 "$local_ref" /usr/local/bin/fs2-image-smoke --build-only 2>&1)" || smoke_rc=$?
  printf '%s\n' "$smoke_output" > "${output_dir}/${id}.smoke.log"
  if [[ "$smoke_rc" -ne 0 ]]; then
    printf 'image smoke failed for %s (exit %s)\n' "$id" "$smoke_rc" >&2
    tail -n 40 "${output_dir}/${id}.smoke.log" >&2
    exit "$smoke_rc"
  fi
  smoke_json="$(tail -n 1 "${output_dir}/${id}.smoke.log")"
  expected_cache_mounts="$(jq -c '.runtime_contract.writable_cache_mounts // []' <<<"$image")"
  expected_cache_environment="$(jq -c '.runtime_contract.cache_environment // {}' <<<"$image")"
  jq -e \
    --arg id "$id" \
    --argjson expected_cache_mounts "$expected_cache_mounts" \
    --argjson expected_cache_environment "$expected_cache_environment" \
    '
      ($expected_cache_mounts + [$expected_cache_environment[]] | unique | sort) as $expected_paths
      | .status == "passed"
        and .runtime_id == $id
        and .artifact_policy == "external-only"
        and .mode == "build-only-not-semantic-readiness"
        and .build_cache.scope == "built-image-filesystem-only"
        and .build_cache.deployment_persistent_mount_readiness == "not-tested"
        and .build_cache.effective_uid == 10001
        and .build_cache.effective_gid == 10001
        and .build_cache.mount_roots == $expected_cache_mounts
        and .build_cache.environment == $expected_cache_environment
        and .build_cache.declared == (($expected_paths | length) > 0)
        and ([.build_cache.directories[].path] | sort) == $expected_paths
        and all(.build_cache.directories[];
          .probe == "bounded-create-read-remove-passed" and .probe_bytes == 25)
    ' <<<"$smoke_json" >/dev/null
  printf '%s\n' "$smoke_json" > "${output_dir}/${id}.smoke.json"

  docker inspect "$local_ref" --format '{{.Architecture}}' | grep -Fx amd64 >/dev/null
  docker inspect "$local_ref" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' | grep -Fx "$revision" >/dev/null
  docker history --no-trunc --format '{{json .}}' "$local_ref" > "${output_dir}/${id}.history.jsonl"

  syft scan "$local_ref" --quiet --output "spdx-json=${output_dir}/${id}.sbom.spdx.json"
  sbom_sha256="$(sha256sum "${output_dir}/${id}.sbom.spdx.json" | awk '{print $1}')"
  published_digest=null

  if [[ "$mode" == publish ]]; then
    state_rc=0
    state="$(target_state "$target")" || state_rc=$?
    if [[ "$state_rc" -eq 0 ]]; then
      printf 'refusing raced overwrite of target %s (%s)\n' "$target" "$state" >&2
      exit 1
    elif [[ "$state_rc" -ne 10 ]]; then
      exit "$state_rc"
    fi
    printf 'TARGET_STATE before_push=absent\n'
    docker tag "$local_ref" "$target"
    docker push "$target"
    published_digest="$(crane digest "$target")"
    crane config "$target" \
      | jq -e --arg revision "$revision" '.config.Labels["org.opencontainers.image.revision"] == $revision' \
      >/dev/null
    printf 'PUBLISHED %s@%s\n' "${target%:*}" "$published_digest"
  fi

  jq -nc \
    --arg id "$id" \
    --arg source_revision "$revision" \
    --arg registry_root "$registry_root" \
    --arg target "$target" \
    --arg local_ref "$local_ref" \
    --argjson smoke "$smoke_json" \
    --arg sbom_sha256 "$sbom_sha256" \
    --arg published_digest "$published_digest" \
    '{id:$id,source_revision:$source_revision,registry_root:$registry_root,target:$target,local_ref:$local_ref,smoke:$smoke,sbom_sha256:$sbom_sha256,published_digest:(if $published_digest == "null" then null else $published_digest end)}' \
    >> "$receipt_lines"
done

jq -s \
  --arg mode "$mode" \
  --arg platform linux/amd64 \
  --arg image_source_revision "$task_head" \
  --arg origin_main_revision "$main_head" \
  --arg merge_base_revision "$merge_base" \
  --arg source_ahead_behind "$ahead_behind" \
  --arg runtime_adapter_revision "$adapter_revision" \
  --arg runtime_adapter_branch "$adapter_branch" \
  '{
    schema:"fs2.nebius.ai/structure-secondary-image-build-receipt/v2",
    mode:$mode,
    platform:$platform,
    image_source_revision:$image_source_revision,
    origin_main_revision:$origin_main_revision,
    merge_base_revision:$merge_base_revision,
    source_ahead_behind:$source_ahead_behind,
    runtime_adapter_revision:$runtime_adapter_revision,
    runtime_adapter_branch:$runtime_adapter_branch,
    images:.
  }' \
  "$receipt_lines" > "${output_dir}/build-receipt.json"
printf 'RECEIPT %s\n' "${output_dir}/build-receipt.json"
