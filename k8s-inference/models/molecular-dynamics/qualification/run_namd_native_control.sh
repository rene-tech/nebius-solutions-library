#!/bin/bash
# Exact public NVE fixture, original equilibrated state; not CUDA-state matched.
set -euo pipefail
source_case=${1:?captured case required}
control_case=${2:?new control directory required}
assigned_gpu=${3:?scheduler-assigned GPU UUID required}
[[ "$source_case" == /checkpoints/* && "$control_case" == /checkpoints/* ]]
[[ "$assigned_gpu" == GPU-* && "$assigned_gpu" != *,* ]]
test ! -e "$control_case"
mkdir "$control_case"
mkdir "$control_case/work"
for native_input in apoa1.psf apoa1.pdb par_all22_prot_lipid.xplor par_all22_popc.xplor equilibrate.coor equilibrate.vel equilibrate.xsc fs2-config-nve.namd fs2-production-part000001.namd; do
    cp -p "$source_case/work/$native_input" "$control_case/work/$native_input"
done
chown -R 10001:10001 "$control_case"
cd "$control_case/work"
sha256sum apoa1.psf apoa1.pdb par_all22_prot_lipid.xplor par_all22_popc.xplor equilibrate.coor equilibrate.vel equilibrate.xsc fs2-config-nve.namd fs2-production-part000001.namd > "$control_case/inputs.sha256"
export CUDA_VISIBLE_DEVICES="$assigned_gpu" NVIDIA_VISIBLE_DEVICES="$assigned_gpu"
TIMEFORMAT=$'process_wall_seconds %R\nuser_seconds %U\nsystem_seconds %S'
{ time timeout --kill-after=10s 180s setpriv --reuid=10001 --regid=10001 --clear-groups /usr/local/namd/bin/namd3 +p4 +devices 0 fs2-production-part000001.namd > "$control_case/worker.log" 2>&1; } 2> "$control_case/native-timing.txt"
python3 /checkpoints/validation/validate_namd_snapshot.py "$control_case" > "$control_case/native-validation.json"
