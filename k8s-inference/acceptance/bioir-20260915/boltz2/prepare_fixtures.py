"""Freeze public BioIR CASP fixtures with their complete supplied MSAs."""
import hashlib
import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent
samples = root / "vendor/bioir/examples/data/samples"
dest = root / "fixtures"
dest.mkdir(exist_ok=True)
records = []
for name in ("T1031", "T1038", "T1096"):
    source = json.loads((samples / f"monomers/{name}.json").read_text())[0]
    polymers = []
    for polymer in source["polymers"]:
        alignment = (samples / "monomers" / polymer["msas"]).read_text()
        polymers.append({"id": "A", "molecule_type": "protein", "sequence": polymer["sequence"],
                         "msa": {"msa_search": {"a3m": {"alignment": alignment, "format": "a3m", "rank": 0}}}})
    request = {"polymers": polymers, "recycling_steps": 3, "sampling_steps": 200,
               "diffusion_samples": 1, "output_format": "mmcif"}
    payload = json.dumps(request, sort_keys=True).encode()
    (dest / f"{name}.json").write_bytes(payload)
    shutil.copyfile(samples / f"gt/{name}.pdb", dest / f"{name}.pdb")
    records.append({"id": name, "length": len(polymers[0]["sequence"]),
                    "msa_rows": alignment.count(">"), "input_sha256": hashlib.sha256(payload).hexdigest(),
                    "reference_sha256": hashlib.sha256((dest / f"{name}.pdb").read_bytes()).hexdigest(),
                    "source_commit": "401c6fcc4a43925bcf1342b6c0979b060130b396"})
complex_source = json.loads((samples / "heterooligomers/7sfy.json").read_text())[0]
complex_polymers = []
for polymer in complex_source["polymers"]:
    alignment = (samples / "heterooligomers" / polymer["msas"][0]["path"]).read_text()
    for chain_id in polymer["chain_id"]:
        complex_polymers.append({"id": chain_id[0], "molecule_type": "protein", "sequence": polymer["sequence"],
                                 "msa": {"msa_search": {"a3m": {"alignment": alignment, "format": "a3m", "rank": 0}}}})
complex_request = {"polymers": complex_polymers, "recycling_steps": 3, "sampling_steps": 200,
                   "diffusion_samples": 1, "output_format": "mmcif"}
(dest / "7sfy.json").write_text(json.dumps(complex_request, sort_keys=True))
shutil.copyfile(samples / "gt/7sfy.cif", dest / "7sfy.cif")
complex_request["polymers"] = [complex_polymers[0], complex_polymers[-1]]
(dest / "7sfy_ac.json").write_text(json.dumps(complex_request, sort_keys=True))
shutil.copyfile(samples / "gt/7sfy.cif", dest / "7sfy_ac.cif")
for name in ("7sfy", "7sfy_ac"):
    payload = (dest / f"{name}.json").read_bytes()
    request = json.loads(payload)
    records.append({"id": name, "length": sum(len(p["sequence"]) for p in request["polymers"]),
                    "chains": [p["id"] for p in request["polymers"]],
                    "input_sha256": hashlib.sha256(payload).hexdigest(),
                    "reference_sha256": hashlib.sha256((dest / f"{name}.cif").read_bytes()).hexdigest(),
                    "source_commit": "401c6fcc4a43925bcf1342b6c0979b060130b396",
                    "limitation": "Ordinary MSAs only; paired MSAs and templates absent. 7sfy_ac omits chainB; flattened CA-lDDT against full complex is exploratory, not DockQ or a complex quality gate."})
(dest / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
print(json.dumps(records, indent=2))
