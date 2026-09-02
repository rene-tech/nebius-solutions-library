#!/usr/bin/env python3
"""Offline contract checks for the cancer-immunotherapy fast-start package."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).parent
TRACKS = {"proteina-complexa","boltzgen","mosaic","rfdiffusion","esmfold2","esmfold2-fast","protenix-v2","alphafold3-native-academic","openfold3-fallback","bindcraft-native-pyrosetta-academic","bindcraft-pyrosetta-free","bindcraft-composite-openfold3","bindcraft-composite-boltzgen"}

def load(p):
    with open(p) as f: return json.load(f)

def validate_documents(matrix, fixtures):
    errors=[]; target=matrix["target"]
    if set(t["track_id"] for t in matrix["tracks"]) != TRACKS: errors.append("track set is incomplete or duplicated")
    if target["local_nvme_model_cache_available"] is not False: errors.append("H100 local NVMe must remain unavailable")
    if target["h100_mutation_gate"] != "wait-for-immutable-runtime-assets-merged": errors.append("live mutation gate changed")
    fixture_ids={f["id"] for f in fixtures["fixtures"]}
    seen=set()
    for t in matrix["tracks"]:
        for u in t["runtime_units"]:
            key=u["evidence_partition_key"]
            if key in seen: errors.append(f"duplicate evidence partition: {key}")
            seen.add(key)
            if u["semantic_fixture_id"] not in fixture_ids: errors.append(f"missing fixture: {u['semantic_fixture_id']}")
            if u["mechanisms"]["node_local_artifact_cache"] != "unavailable-platform": errors.append(f"node-local cache claimed for {u['unit_id']}")
            if u["ready_for_live_trials"]: errors.append(f"live trial enabled before gate: {u['unit_id']}")
            snap=u["snapshot"]
            if snap["state"] == "supported": errors.append(f"unproven snapshot support: {u['unit_id']}")
            if snap["state"] == "gated" and snap["backend"] != "cuda-checkpoint+criu": errors.append(f"snapshot backend must name CUDA checkpoint + CRIU: {u['unit_id']}")
        if t["track_id"] == "openfold3-fallback" and t["lane_kind"] != "fallback": errors.append("OpenFold3 must be fallback lane")
    ids={t["track_id"] for t in matrix["tracks"]}
    if "alphafold3-native-academic" not in ids or "openfold3-fallback" not in ids: errors.append("AF3/OpenFold3 lanes must remain distinct")
    for f in fixtures["fixtures"]:
        if f.get("request_count") != 2 or f.get("request_payloads_embedded") or f.get("licensed_payloads_embedded") or f.get("credential_material_embedded"): errors.append(f"fixture safety violation: {f['id']}")
    return errors

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument("--matrix",default=ROOT/"qualification-matrix.json"); ap.add_argument("--fixtures",default=ROOT/"semantic-fixtures.json"); args=ap.parse_args(argv)
    errors=validate_documents(load(args.matrix),load(args.fixtures))
    print(json.dumps({"valid":not errors,"errors":errors},indent=2))
    return 2 if errors else 0
if __name__ == "__main__": sys.exit(main())
