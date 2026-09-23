"""Substantive enhanced-sampling fixtures from the qualified solvated protein.

These bias a CA radius of gyration to exercise forces, time-dependent bias,
segmented native continuation and analysis. They are not converged free energies
or a proposed customer scientific protocol.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extension", choices=("colvars", "plumed"), required=True)
    parser.add_argument("--steps", type=int, default=100000)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
    gro = (source / "nvt.gro").read_text().splitlines()
    cas = [
        index for index, line in enumerate(gro[2:-1], 1) if line[10:15].strip() == "CA"
    ]
    if len(cas) != 129:
        raise ValueError(
            "Expected all 129 protein CA atoms from the qualified 1AKI fixture"
        )
    mdp = (source / "md.mdp").read_text()
    mdp = "\n".join(
        line for line in mdp.splitlines() if not line.strip().startswith("nsteps")
    )
    mdp += f"\nnsteps = {args.steps}\n"
    bias = {}
    if args.extension == "colvars":
        mdp += "colvars-active = true\ncolvars-configfile = colvars.dat\ncolvars-seed = 20260923\n"
        bias["colvars.dat"] = (
            "units gromacs\ncolvarsTrajFrequency 1000\ncolvarsRestartFrequency 1000\n"
            "colvar {\n name rg\n width 0.02\n gyration {\n  atoms {\n   atomNumbers "
            + " ".join(map(str, cas))
            + "\n  }\n }\n}\n"
            "metadynamics {\n colvars rg\n hillWeight 0.01\n gaussianSigmas 0.02\n newHillFrequency 1000\n"
            " wellTempered on\n biasTemperature 2700\n}\n"
        )
    else:
        bias["plumed.dat"] = (
            "rg: GYRATION ATOMS=" + ",".join(map(str, cas)) + "\n"
            "metad: METAD ARG=rg SIGMA=0.02 HEIGHT=0.01 PACE=1000 BIASFACTOR=10 TEMP=300 FILE=HILLS\n"
            "PRINT ARG=rg,metad.bias STRIDE=1000 FILE=COLVAR\n"
        )
    included = ["topol.top", "nvt.gro", "fs2-equilibrate.cpt"]
    included += [
        str(path.relative_to(source)) for path in sorted(source.rglob("*.itp"))
    ]
    import io

    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for name in included:
            archive.add(source / name, arcname=name, recursive=False)
        for name, value in {"advanced.mdp": mdp, **bias}.items():
            content = value.encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    production = {
        "id": "production",
        "command": "mdrun",
        "args": ["-s", "md.tpr", "-deffnm", "md"],
    }
    if args.extension == "plumed":
        production["plumed_input"] = "plumed.dat"
        production["expected_outputs"] = ["HILLS", "COLVAR"]
    steps = [
        {
            "id": "prepare",
            "command": "grompp",
            "args": [
                "-f",
                "advanced.mdp",
                "-c",
                "nvt.gro",
                "-t",
                "fs2-equilibrate.cpt",
                "-p",
                "topol.top",
                "-o",
                "md.tpr",
            ],
        },
        production,
        {
            "id": "join",
            "command": "trjcat",
            "args": ["-f", {"files": "md.part*.xtc"}, "-o", "md.xtc"],
        },
        {"id": "check", "command": "check", "args": ["-f", "md.xtc"]},
        {
            "id": "gyration",
            "command": "gyrate",
            "args": [
                "-s",
                "md.tpr",
                "-f",
                "md.xtc",
                "-sel",
                "name CA",
                "-o",
                "gyration.xvg",
            ],
        },
    ]
    request = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "jobs": [{"id": args.extension, "steps": steps}],
        "segment_minutes": 0.1,
        "checkpoint_minutes": 0.1,
        "max_wall_seconds": 3600,
    }
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    (args.output / "fixture.json").write_text(
        json.dumps(
            {
                "extension": args.extension,
                "source": "RCSB 1AKI / qualified AMBER99SB-ILDN TIP3P preparation",
                "input_sha256": hashlib.file_digest(
                    (args.output / "input.tar.gz").open("rb"), "sha256"
                ).hexdigest(),
                "steps": args.steps,
                "protein_ca_atoms": len(cas),
                "convergence_claimed": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
