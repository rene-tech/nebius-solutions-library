#!/usr/bin/env python3
"""Extend a verified immutable starter pack with canonical MD input recipes.

Binary data are built into the data image, never committed. Original simulations
and earlier pack versions are read-only. A generated pack is always a draft.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

import run_example as runner

MODELS = ("gromacs", "namd", "amber", "lammps")
BASE_SHA = "9387cfd01e71fdfce67e08008ebfccd359082e60485ecf30b4f17a30d4df22b6"
INPUT_SHA = {
    "amber": "ee38fc8a129c2519ba8c9699823114930d84249ff96d481a2aaa8b78e753a481",
    "gromacs": "b060fb630d83ba1501b7f3e3a3b592c38f9e8633167936f95056c04681004caf",
    "lammps": "cb0ab63bd6311acf030288db41544d7ea3988e47b7155da2dd62f18808162b53",
    "namd": "79d2ef59dd92512873b5f36a7fc36598ca921ec8dae3af5f24759673df6064fe",
}
REQUEST_SHA = {
    "amber": "2f9e14327c5bbe35f95b44f063659a5f9d17571bbff91f8369656936a1017a9e",
    "gromacs": "600149a59698fef2bacbfa1f291f0935e971cd403f4af0c23e981f188b88a10a",
    "lammps": "1d5b8f866487c6ff30a396f9753f67bb85cecb3b9b0626bfae30772f777cb091",
    "namd": "26f13477e232fc5015d5d4bcc73dc3470d4f5ff57fa3c4f5bd1487d48546e9d1",
}
DELIVERY_SHA = "e9df4cee5404587cbed77387e7abb52636720be37e10412e75e8d6718e40e01e"
PROVENANCE = {
    "source": "Nebius four-engine alanine delivery-02, 2026-09-23; source aa87df00599afe881f2d14c1d6bc935e27f71c52",
    "license": "Apache-2.0 authored scripts and generated demo coordinates; public-domain Amber force-field parameters",
    "attribution": "Nebius Scientific AI; AmberTools authors; Maier et al. ff14SB; Jorgensen et al. TIP3P",
    "transformation": "Synthetic ACE-ALA-NME + 2192 TIP3P waters, 6598 atoms; prepared with AmberTools and validated converted topology; not customer data",
}


def unpack(data):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            if (
                not entry.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or entry.name in files
            ):
                raise ValueError("invalid_input_archive_member")
            files[entry.name] = archive.extractfile(entry).read()
    return files


def archive(files):
    output = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as tar,
    ):
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


def replace_once(text, pattern, replacement):
    value, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("native_setting_not_unique_" + pattern)
    return value


def quick_variant(model, files, request):
    """Explicit 20 ps NVT + 20 ps NPT + 20 ps production; physics unchanged."""
    files, request = dict(files), copy.deepcopy(request)
    for name, raw in list(files.items()):
        text = None
        if model == "gromacs" and name in ("nvt.mdp", "npt.mdp", "production.mdp"):
            text = replace_once(
                raw.decode(), r"^nsteps\s*=\s*\d+\s*$", "nsteps = 10000"
            )
        elif model == "amber" and name in ("nvt.in", "npt.in", "production-001.in"):
            text = replace_once(raw.decode(), r"\bnstlim=\d+", "nstlim=10000")
        elif model == "namd" and name == "alanine/nvt.namd":
            text = replace_once(raw.decode(), r"^run 50000$", "run 10000")
        elif model == "lammps" and name in (
            "nvt.in",
            "nvt-resume.in",
            "npt.in",
            "npt-resume.in",
            "production.in",
            "production-resume.in",
        ):
            text = raw.decode()
            target = (
                10000
                if name.startswith("nvt")
                else 20000
                if name.startswith("npt")
                else 30000
            )
            text = replace_once(text, r"^run \d+ upto$", f"run {target} upto")
        if text is not None:
            files[name] = text.encode()
    steps = request["jobs"][0]["steps"]
    if model == "amber":
        for step in steps:
            if "expected_nsteps" in step:
                step["expected_nsteps"] = 10000
    if model == "namd":
        for step in steps:
            if step["id"] in ("npt", "production"):
                step.update(
                    steps=10000,
                    segment_steps=10000,
                    first_step=15000 if step["id"] == "npt" else 25000,
                )
    if model == "lammps":
        for step in steps:
            if "continuation" in step:
                step["continuation"]["target_step"] = {
                    "nvt": 10000,
                    "npt": 20000,
                    "production": 30000,
                }[step["id"]]
    # Original protocol documents are historical provenance, not the new run plan.
    files["starter-protocol.json"] = runner.encoded(
        {
            "variant": "introductory",
            "nvt_ps": 20,
            "npt_ps": 20,
            "production_ps": 20,
            "timestep_fs": 2,
            "output_interval_ps": 1,
            "convergence_claimed": False,
            "other_protocol_files": "Retained canonical source provenance; starter-protocol.json and native controls define this shorter variant.",
        }
    )
    return files, request


def resume_variant(model, files, request, read):
    """Continue a retained 1 ns native state for 20 ps; never reinitialize it."""
    files, request = dict(files), copy.deepcopy(request)
    steps = []
    expected = {"production_ps": 20, "atoms": 6598, "jobs": 1, "native_restart": True}
    if model == "gromacs":
        files = {
            "source.tpr": read("runs/gromacs/data/production.tpr"),
            "source.cpt": read("runs/gromacs/data/fs2-production.cpt"),
            "system.gro": files["system.gro"],
            "system.top": files["system.top"],
        }
        steps = [
            {
                "id": "extend",
                "command": "convert-tpr",
                "args": ["-s", "source.tpr", "-extend", "20", "-o", "extended.tpr"],
                "expected_outputs": ["extended.tpr"],
            },
            {
                "id": "production",
                "command": "mdrun",
                "args": ["-s", "extended.tpr", "-deffnm", "production", "-notunepme"],
                "restart_checkpoint": "source.cpt",
                "expected_outputs": ["production.gro"],
            },
            {
                "id": "trajectory",
                "command": "trjcat",
                "args": [
                    "-f",
                    {"files": "production*.xtc"},
                    "-o",
                    "canonical-production.xtc",
                ],
                "expected_outputs": ["canonical-production.xtc"],
            },
        ]
        expected["first_step"] = 500000
    elif model == "amber":
        files["source.rst7"] = read("runs/amber/data/production-001.rst7")
        files["continue.in"] = replace_once(
            files["production-001.in"].decode(), r"\bnstlim=500000", "nstlim=10000"
        ).encode()
        steps = [
            {
                "id": "production",
                "input": "continue.in",
                "topology": "system.prmtop",
                "coordinates": "source.rst7",
                "expected_nsteps": 10000,
                "output_prefix": "production-001",
                "expected_outputs": ["production-001.nc"],
            }
        ]
        expected["first_step"] = (
            0  # AMBER resets step counter while retaining physical time.
        )
    elif model == "namd":
        for suffix in ("coor", "vel", "xsc"):
            files[f"alanine/source.{suffix}"] = read(
                f"runs/namd/data/alanine/production.{suffix}"
            )
        first = int(
            next(
                line.split()[0]
                for line in files["alanine/source.xsc"].decode().splitlines()
                if line and not line.startswith("#")
            )
        )
        step = copy.deepcopy(request["jobs"][0]["steps"][-1])
        step.update(
            steps=10000,
            segment_steps=10000,
            first_step=first,
            restart={
                "coordinates": "source.coor",
                "velocities": "source.vel",
                "cell": "source.xsc",
            },
        )
        steps = [step]
        expected["first_step"] = first
    else:
        files["production.restart"] = read("runs/lammps/data/production.restart")
        files["continue.in"] = replace_once(
            files["production-resume.in"].decode(),
            r"^run 600000 upto$",
            "run 610000 upto",
        ).encode()
        steps = [
            {
                "id": "production",
                "input": "continue.in",
                "expected_outputs": ["production.restart", "production-progress.txt"],
                "continuation": {
                    "input": "continue.in",
                    "restart_file": "production.restart",
                    "progress_file": "production-progress.txt",
                    "target_step": 610000,
                },
            }
        ]
        expected["first_step"] = 600000
    request["jobs"] = [{"id": "continue-native-state", "steps": steps}]
    files["starter-protocol.json"] = runner.encoded(
        {
            **expected,
            "variant": "native-restart",
            "source": "retained completed canonical 1 ns state",
            "gpu_snapshot": False,
            "bitwise_continuation_claimed": False,
        }
    )
    return files, request, expected


def batch_variant(model, files, request):
    """Two genuinely independent random-seed replicas, not duplicate encodings."""
    bundle, request = {}, copy.deepcopy(request)
    original = request["jobs"][0]
    request["jobs"] = []
    for index in range(2):
        prefix = f"replica-{index + 1}"
        changed = dict(files)
        for name, raw in list(changed.items()):
            if name.endswith((".mdp", ".in", ".namd")):
                text = raw.decode()
                # These source seeds are explicit in the retained canonical protocol.
                for offset, seed in enumerate((20260923, 20260924, 20260925)):
                    text = re.sub(
                        r"\b" + str(seed) + r"\b",
                        str(202609250 + 3 * index + offset),
                        text,
                    )
                changed[name] = text.encode()
        for name, data in changed.items():
            bundle[prefix + "/" + name] = data
        job = copy.deepcopy(original)
        job["id"] = prefix
        for step in job["steps"]:
            previous = step.get("directory", ".")
            step["directory"] = prefix if previous == "." else prefix + "/" + previous
        request["jobs"].append(job)
    return (
        bundle,
        request,
        {
            "production_ps": 20,
            "nvt_ps": 20,
            "npt_ps": 20,
            "atoms": 6598,
            "jobs": 2,
            "independent_seeds": [202609250, 202609253],
        },
    )


class Pack:
    def __init__(self, base, output):
        self.root = output
        raw = (base / "manifest.json").read_bytes()
        if runner.sha(raw) != BASE_SHA:
            raise ValueError("unreviewed_base_pack")
        self.manifest = json.loads(raw)
        output.mkdir(parents=True, exist_ok=False)
        inputs = runner.Inputs(base, self.manifest, runner.offline_upload)
        self.objects = {}
        for item in self.manifest["objects"]:
            content = inputs.read(item["path"])
            path = output / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.objects[item["path"]] = copy.deepcopy(item)
        self.manifest.update(
            version="v3",
            release_status="draft",
            qualification={"state": "pending", "receipts": []},
        )
        self.manifest["categories"].append(
            {
                "id": "molecular-dynamics",
                "display_name": "Molecular dynamics",
                "minimum_cases": 5,
                "variety": "five workflow examples sharing one canonical molecule; not five independent molecular systems",
            }
        )
        self.manifest["live_model_ids"] = sorted(
            set(self.manifest["live_model_ids"]) | set(MODELS)
        )

    def add(self, name, value, media="application/json", models=()):
        data = (
            value
            if isinstance(value, bytes)
            else value.encode()
            if isinstance(value, str)
            else runner.encoded(value)
        )
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("starter_object_too_large")
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.objects[name] = {
            "path": name,
            "sha256": runner.sha(data),
            "size_bytes": len(data),
            "media_type": media,
            "compatible_model_ids": list(models),
            "recipe_version": "1",
            "validation_status": "pending-live",
            "provenance": PROVENANCE,
        }
        return name

    def case(self, slug, title, description, variants, contracts):
        identifier = "molecular-dynamics/" + slug
        recipes, assets, expectations = [], [], {}
        for model, (files, parameters, expected) in variants.items():
            prefix = identifier + "/" + model
            bundle = self.add(
                prefix + "/input.tar.gz", archive(files), "application/gzip", [model]
            )
            parameters = copy.deepcopy(parameters)
            parameters.update(
                output_destination="customer-bucket",
                output_prefix="runs/starter-md/" + slug + "/" + model,
            )
            self.add(prefix + "/parameters.json", parameters, models=[model])
            entries = [
                {
                    "name": model + "-inputs",
                    "semantic_type": model + "-input-bundle/v1",
                    "artifact": {
                        "$file": bundle,
                        "encoding": "artifact",
                        "media_type": "application/x-tar",
                        "compression": "gzip",
                    },
                }
            ]
            template = self.add(
                prefix + "/input-manifest.template.json",
                {
                    "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                    "manifest_id": "starter.v3." + slug + "." + model,
                    "entries": entries,
                },
                models=[model],
            )
            contract = contracts[model]["contracts"][0]
            recipe = {
                "model_id": model,
                "protocol": contract["protocol"],
                "tool_name": contract["tool_name"],
                "contract_sha256": runner.sha(runner.encoded(contract)),
                "download_budget_bytes": 2 * 1024**3,
                "download_max_artifacts": 1024,
                "arguments": {
                    "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                    "operation": "run-workflow",
                    "service_class": "customer-batch",
                    "input_manifest": {"$manifest": template},
                    "parameters": parameters,
                    "client_context": {
                        "batch_id": "starter.v3." + slug + "." + model,
                        "correlation_id": "starter.v3.md",
                        "display_name": title + " / " + model,
                    },
                },
            }
            recipes.append(recipe)
            assets += [bundle, prefix + "/parameters.json", template]
            expectations[model] = expected
        recipe_path = self.add(
            identifier + "/recipes.json",
            {
                "schema": "fs2-serve.nebius.ai/starter-recipes/v1",
                "case_id": identifier,
                "recipes": recipes,
                "expected": {
                    "kind": "molecular-dynamics",
                    "description": description,
                    "engines": expectations,
                },
            },
            models=variants,
        )
        readme = f"# {title}\n\n{description}\n\n" + (
            "Download the whole `examples/v3/` prefix. From its root, use your own API key and public MCP URL:\n\n"
            f"```sh\npython run-example.py {identifier} --model {next(iter(variants))} --output ../runs/{slug}\n```\n\n"
            "Use `--model gromacs`, `--model namd`, `--model amber` or `--model lammps` where this case supports it. "
            "No engine is installed on the client: computation runs on the platform. The runner uploads the local archive, polls "
            "the same durable operation, verifies every downloaded artifact and resumes when the same output path is reused. "
            "Results also go to your assigned customer bucket under `runs/starter-md/`. Keep enough bucket quota/headroom for repeated runs; "
            "native text trajectories can be much larger than compressed inputs.\n\n"
            "The full protocol previously produced roughly 0.1–1 GB per engine. Allow 2 GB local result space per invocation, "
            "plus bucket space; introductory runs are smaller. Queue/startup time depends on available capacity. "
            "Measured durations are added only after live qualification.\n\n"
            "Engine-native restart files are not CUDA/GPU snapshots. Stochastic trajectories are not expected to match frame by frame. "
            "Short runs teach operation of the service and do not establish equilibrium, force equivalence or converged free energies. "
            "See `../README.md` for method caveats, attribution and analysis.\n\n"
            f"LibreChat prompt: **Run the {title.lower()} example from my bucket's examples/v3/{identifier}/ using "
            f"{next(iter(variants)).upper()}. Follow its README and packaged native parameters, save all outputs into my bucket, "
            "and report the operation ID, completion checks and limitations.**\n"
        )
        self.add(identifier + "/README.md", readme, "text/markdown", variants)
        self.manifest["cases"].append(
            {
                "id": identifier,
                "category": "molecular-dynamics",
                "title": title,
                "compatible_model_ids": list(variants),
                "assets": assets,
                "recipes": recipe_path,
                "validation_status": "pending-live",
                "expected": {
                    "kind": "molecular-dynamics",
                    "description": description,
                    "engines": expectations,
                },
            }
        )

    def finish(self):
        self.add(
            "run-example.py",
            Path(__file__).with_name("run_example.py").read_bytes(),
            "text/x-python",
        )
        current = (
            (self.root / "README.md").read_text().replace("examples/v2", "examples/v3")
        )
        self.add(
            "README.md",
            current
            + "\n## Molecular dynamics\n\n[Four-engine inputs and workflows](molecular-dynamics/README.md). Only explicitly selected examples run; seeding consumes no GPU time.\n",
            "text/markdown",
        )
        cases = [
            c for c in self.manifest["cases"] if c["category"] == "molecular-dynamics"
        ]
        self.add(
            "molecular-dynamics/README.md",
            "# Molecular dynamics\n\n"
            + "\n".join(
                f"- [{c['title']}]({c['id'].split('/', 1)[1]}/README.md)" for c in cases
            )
            + "\n\nOne canonical ACE–ALA–NME molecule, 2192 TIP3P waters, 6598 atoms, ff14SB. "
            "Full protocol: minimization, 100 ps NVT, 100 ps NPT and 1 ns NPT, 300 K, 1 bar, 2 fs, 1 ps trajectory output. "
            "Native integrators/barostats differ; topology conversion was independently checked, not inferred from process success. "
            "No convergence, identical trajectories or general four-engine force equivalence is claimed. Amber NVT pressure is unavailable, "
            "not zero; its midpoint temperature estimator differs from instantaneous velocity-based temperature. "
            "LAMMPS native SHAKE restart can reproject coordinates.\n\n"
            "Authored scripts and synthetic demo coordinates: Apache-2.0. Amber force fields are public domain; "
            "see Case et al., [AmberTools](https://doi.org/10.1021/acs.jcim.3c01153). "
            "Cite [ff14SB](https://doi.org/10.1021/acs.jctc.5b00255) and [TIP3P](https://doi.org/10.1063/1.445869). "
            "No engine binaries, credentials or customer data are included. Model access and engine licensing still apply.\n",
            "text/markdown",
            MODELS,
        )
        self.manifest["objects"] = list(self.objects.values())
        (self.root / "manifest.json").write_bytes(runner.encoded(self.manifest))
        print(
            json.dumps(
                {
                    "output": str(self.root),
                    "new_cases": len(cases),
                    "new_recipes": sum(len(c["compatible_model_ids"]) for c in cases),
                    "total_bytes": sum(o["size_bytes"] for o in self.objects.values()),
                    "status": "draft",
                }
            )
        )


def build(args):
    pack = Pack(args.base, args.output)
    contracts = {
        m: json.loads((args.contracts / (m + ".json")).read_bytes()) for m in MODELS
    }
    originals, quick = {}, {}
    raw_manifest = (args.delivery / "delivery-manifest.json").read_bytes()
    if runner.sha(raw_manifest) != DELIVERY_SHA:
        raise ValueError("canonical_delivery_manifest_changed")
    source_files = {item["path"]: item for item in json.loads(raw_manifest)["files"]}

    def read(relative):
        data = (args.delivery / relative).read_bytes()
        if (
            runner.sha(data) != source_files[relative]["sha256"]
            or len(data) != source_files[relative]["bytes"]
        ):
            raise ValueError("canonical_delivery_file_changed")
        return data

    for model in MODELS:
        source = args.delivery / "inputs" / model
        data, raw = (
            (source / "input.tar.gz").read_bytes(),
            (source / "request.json").read_bytes(),
        )
        if (
            runner.sha(data) != INPUT_SHA[model]
            or runner.sha(raw) != REQUEST_SHA[model]
        ):
            raise ValueError("canonical_source_changed_" + model)
        files, request = unpack(data), json.loads(raw)
        originals[model] = (
            files,
            request,
            {
                "production_ps": 1000,
                "nvt_ps": 100,
                "npt_ps": 100,
                "atoms": 6598,
                "jobs": 1,
            },
        )
        short_files, short_request = quick_variant(model, files, request)
        quick[model] = (
            short_files,
            short_request,
            {"production_ps": 20, "nvt_ps": 20, "npt_ps": 20, "atoms": 6598, "jobs": 1},
        )
    pack.case(
        "alanine-quickstart",
        "Alanine introductory run",
        "Minimize; 20 ps NVT, 20 ps NPT and 20 ps production. Functional onboarding only, not equilibrated scientific sampling.",
        quick,
        contracts,
    )
    pack.case(
        "alanine-1ns",
        "Alanine full 1 ns comparison",
        "Minimize; 100 ps NVT, 100 ps NPT and 1 ns production for the canonical ff14SB/TIP3P system. Validate native step/frame counts, coordinates and thermodynamics; compare ensembles, not matching frames.",
        originals,
        contracts,
    )
    pack.case(
        "alanine-restart",
        "Alanine native restart",
        "Continue the included completed 1 ns native state for 20 ps, preserving coordinates, velocities and periodic cell. This demonstrates portable native checkpoint continuation, not GPU snapshotting or bitwise reproduction.",
        {m: resume_variant(m, f, r, read) for m, (f, r, _) in originals.items()},
        contracts,
    )
    pack.case(
        "alanine-replicas",
        "Two independent alanine replicas",
        "Submit two isolated jobs with different recorded seeds. Each minimizes and runs 20 ps NVT, 20 ps NPT and 20 ps production. Jobs may queue; two replicas do not establish converged populations.",
        {m: batch_variant(m, f, r) for m, (f, r, _) in quick.items()},
        contracts,
    )
    if args.umbrella:
        # This is a complete single-window tutorial, not a fabricated global PMF.
        root = args.umbrella / "window-00"
        prep = json.loads((root / "preparation.json").read_bytes())
        data, raw = (
            (root / "input.tar.gz").read_bytes(),
            (root / "request.json").read_bytes(),
        )
        if (
            runner.sha(data) != prep["bundle"]["sha256"]
            or runner.sha(raw) != prep["request"]["sha256"]
        ):
            raise ValueError("umbrella_source_changed")
        pack.case(
            "alanine-umbrella-window",
            "Alanine dihedral umbrella window",
            "Opt-in enhanced-sampling tutorial: one native phi window at -180 degrees, 200 kJ/mol/rad^2 restraint, 100 ps NVT + 100 ps NPT + 2 ns production. Retains phi and unbiased psi observations. One window cannot reconstruct a global PMF; all 24 windows and WHAM are an explicitly larger study.",
            {
                "gromacs": (
                    unpack(data),
                    json.loads(raw),
                    {
                        "production_ps": 2000,
                        "nvt_ps": 100,
                        "npt_ps": 100,
                        "atoms": 6598,
                        "jobs": 1,
                        "umbrella": True,
                    },
                )
            },
            contracts,
        )
    for name in (
        "master/system.prmtop",
        "master/system.rst7",
        "master/system.pdb",
        "master/prepare.leap",
        "master/protocol.json",
        "master/master-manifest.json",
    ):
        pack.add(
            "molecular-dynamics/canonical/" + name.split("/", 1)[1],
            read(name),
            "application/octet-stream",
            MODELS,
        )
    for name in ("native.py", "geometry.py"):
        pack.add(
            "molecular-dynamics/analysis/" + name,
            read("analysis-inputs/code/" + name),
            "text/x-python",
            MODELS,
        )
    pack.add(
        "molecular-dynamics/analysis-requirements.txt",
        read("analysis-inputs/requirements.txt"),
        "text/plain",
        MODELS,
    )
    pack.add(
        "molecular-dynamics/analyze-md.py",
        Path(__file__).with_name("md_analyze.py").read_bytes(),
        "text/x-python",
        MODELS,
    )
    pack.add(
        "molecular-dynamics/source-provenance.json",
        {
            "delivery_manifest_sha256": DELIVERY_SHA,
            "source_commit": "aa87df00599afe881f2d14c1d6bc935e27f71c52",
            "canonical_input_sha256": INPUT_SHA,
            "canonical_request_sha256": REQUEST_SHA,
            "inherited_starter_manifest_sha256": BASE_SHA,
            "reference_outputs_seeded": False,
            "engine_binaries_seeded": False,
        },
    )
    pack.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--umbrella", type=Path)
    build(parser.parse_args())
