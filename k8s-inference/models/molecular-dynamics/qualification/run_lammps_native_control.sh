#!/bin/bash
# Exact synthetic LJ request; ordinary native workflow, not a CUDA restore.
set -euo pipefail
source_case=${1:?captured case required}
control_case=${2:?new control directory required}
assigned_gpu=${3:?scheduler-assigned GPU UUID required}
[[ "$source_case" == /checkpoints/* && "$control_case" == /checkpoints/* ]]
[[ "$assigned_gpu" == GPU-* && "$assigned_gpu" != *,* ]]
test ! -e "$control_case"
mkdir "$control_case"
mkdir "$control_case/work"
cp -p "$source_case/work/input.tar.gz" "$source_case/work/request.json" "$control_case/work/"
chown -R 10001:10001 "$control_case"
cd "$control_case/work"
sha256sum input.tar.gz request.json > "$control_case/inputs.sha256"
export CUDA_VISIBLE_DEVICES="$assigned_gpu" NVIDIA_VISIBLE_DEVICES="$assigned_gpu"
TIMEFORMAT=$'process_wall_seconds %R\nuser_seconds %U\nsystem_seconds %S'
{ time timeout --kill-after=10s 300s setpriv --reuid=10001 --regid=10001 --clear-groups python3 -m fs2_lammps.worker --request "$control_case/work/request.json" --workspace "$control_case/work" --job-id lj --operation-id 809569b4-0146-4c05-932b-8ab6f40c2f66 --checkpoint-mode local > "$control_case/worker.log" 2>&1; } 2> "$control_case/native-timing.txt"
python3 /checkpoints/validation/validate_lammps.py "$control_case/work" --output "$control_case/native-validation.json"
