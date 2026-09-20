#!/usr/bin/env python3
"""Build a bounded, provenance-bearing starter pack; never publish it qualified.

Generated binary assets belong in the release artifact, not the source tree.
Public source checksums are retained in the generated manifest. Qualification
is a separate step against the exact customer endpoint and every recipe.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import random
import subprocess
import tarfile
import urllib.request
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SCHEMA = "fs2-serve.nebius.ai/customer-starter-pack/v1"
LABELS = {
    "structure": "Structure prediction & docking",
    "protein-design": "Protein design",
    "genomics": "Genomics",
    "small-molecule": "Small-molecule generation",
    "sequence-search": "Sequence search",
    "imaging": "Bio-imaging",
    "single-cell": "Single-cell analysis",
    "age-prediction": "Biological age prediction",
    "physical-ai-robotics": "Physical AI and Robotics",
    "speech": "Speech & audio",
    "general-ai": "General-Purpose",
}
AUTHORED = {
    "source": "Nebius Scientific AI starter-data build_pack.py",
    "license": "Apache-2.0",
    "attribution": "Nebius Scientific AI contributors",
    "transformation": "original synthetic educational fixture; not patient or experimental data",
}
PDB_POLICY = "https://www.rcsb.org/pages/usage-policy"


def json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def file_ref(
    path,
    *,
    encoding="artifact",
    media_type="application/octet-stream",
    compression="none",
):
    return {
        "$file": path,
        "encoding": encoding,
        "media_type": media_type,
        "compression": compression,
    }


class Builder:
    def __init__(self, args):
        self.args, self.root = args, args.output
        self.root.mkdir(parents=True, exist_ok=False)
        self.objects, self.cases = {}, []
        self.source_lock = json.loads(
            Path(__file__).with_name("source-lock.json").read_bytes()
        )
        self.contracts = {}
        self.input_contracts = {}
        self.catalog = json.loads(
            (args.contracts / "website-catalog.json").read_bytes()
        )
        self.metadata = json.loads(
            (args.contracts / "website-metadata.json").read_bytes()
        )
        self.models = {model["id"] for model in self.catalog["models"]}
        for model in self.models:
            path = args.contracts / (model + ".json")
            if path.exists():
                schema = json.loads(path.read_bytes())
                self.contracts[model] = schema["contracts"]
                self.input_contracts[model] = schema.get("input_artifact_contract")

    def add(
        self, path, content, media="application/json", *, provenance=None, models=()
    ):
        if not isinstance(content, bytes):
            content = (
                content.encode() if isinstance(content, str) else json_bytes(content)
            )
        if path in self.objects:
            assert self.objects[path]["sha256"] == hashlib.sha256(content).hexdigest()
            self.objects[path]["compatible_model_ids"] = sorted(
                set(self.objects[path]["compatible_model_ids"]) | set(models)
            )
            return path
        assert len(content) <= 32 * 1024 * 1024
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.objects[path] = {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media,
            "compatible_model_ids": sorted(models),
            "recipe_version": "1",
            "validation_status": "format-validated",
            "provenance": provenance or AUTHORED,
        }
        return path

    def fetch(self, url, cache_name):
        path = self.args.cache / "sources" / cache_name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with urllib.request.urlopen(url, timeout=90) as response:
                data = response.read(256 * 1024 * 1024 + 1)
            assert len(data) <= 256 * 1024 * 1024
            path.write_bytes(data)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.source_lock[cache_name]:
            raise ValueError(
                "Public source differs from the reviewed checksum: " + cache_name
            )
        return data

    def recipe(self, model, payload, *, protocol="native", tool=None):
        assert model in self.models
        contracts = [
            c
            for c in self.contracts.get(model, [])
            if c["protocol"] == protocol and (tool is None or c["tool_name"] == tool)
        ]
        if len(contracts) != 1:
            raise ValueError(
                f"Select one actual published contract: {model} / {protocol} / {tool}"
            )
        return {
            "model_id": model,
            "protocol": protocol,
            "tool_name": contracts[0]["tool_name"],
            "arguments": payload,
            "contract_sha256": hashlib.sha256(json_bytes(contracts[0])).hexdigest(),
        }

    def case(
        self,
        category,
        slug,
        title,
        description,
        recipes,
        expected,
        *,
        assets=(),
        reference_note=None,
    ):
        identifier = f"{category}/{slug}"
        models = sorted({r["model_id"] for r in recipes})
        recipe_path = identifier + "/recipes.json"
        self.add(
            recipe_path,
            {
                "schema": "fs2-serve.nebius.ai/starter-recipes/v1",
                "case_id": identifier,
                "recipes": recipes,
                "expected": expected,
            },
            models=models,
        )
        for path in assets:
            self.objects[path]["compatible_model_ids"] = sorted(
                set(self.objects[path]["compatible_model_ids"]) | set(models)
            )
        readme = (
            f"# {title}\n\n{description}\n\n"
            f"Models: {', '.join(models)}.\n\n"
            f"Run from the downloaded pack root: `python run-example.py {identifier} --output runs/{slug}`. "
            "Select a model with `--model MODEL_ID`. Use your own platform key in "
            "`SCIENTIFIC_MODELS_API_KEY`; never put it in a recipe.\n\n"
            f"Expected: {expected['description']}\n\n"
            "Results are predictions, not validated scientific or clinical ground truth. "
            "Stochastic results are checked structurally, not with byte-for-byte equality.\n\n"
            + ((reference_note + "\n\n") if reference_note else "")
            + "Timing: not benchmarked yet; the validation receipt must provide measured "
            "queue/startup/runtime for this release. Cold models may wait for GPU capacity.\n"
        )
        self.add(identifier + "/README.md", readme, "text/markdown", models=models)
        self.cases.append(
            {
                "id": identifier,
                "category": category,
                "title": title,
                "compatible_model_ids": models,
                "assets": list(assets),
                "recipes": recipe_path,
                "validation_status": "pending-live",
                "expected": expected,
            }
        )

    def finish(self):
        self.add(
            "run-example.py",
            Path(__file__).with_name("run_example.py").read_bytes(),
            "text/x-python",
        )
        self.add(
            "requirements.txt",
            "httpx2==2.12.0\nmcp==2.1.1\njsonschema==4.26.0\n",
            "text/plain",
        )
        self.add(
            "licenses/Apache-2.0.txt",
            (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes(),
            "text/plain",
        )
        for category, label in LABELS.items():
            cases = [c for c in self.cases if c["category"] == category]
            text = (
                f"# {label}\n\n"
                + "\n".join(
                    f"- [{c['title']}]({c['id'].split('/', 1)[1]}/README.md)"
                    for c in cases
                )
                + "\n"
            )
            self.add(category + "/README.md", text, "text/markdown")
        self.add(
            "README.md",
            "# Scientific AI starter data\n\n"
            "Licensed public data and explicitly synthetic demonstrations, organized like the live catalog. "
            "No customer uploads are included. Never use these examples or model outputs for clinical decisions.\n\n"
            + "\n".join(
                f"- [{label}]({category}/README.md)"
                for category, label in LABELS.items()
            )
            + "\n\n"
            "Download the entire examples/v1 prefix before running recipes. Object Storage access keys "
            "download files; the platform API key authorizes inference. They are different credentials. "
            "Existing files are never overwritten by the installer. After successful installation, "
            "deleting an example does not cause the platform to recreate it.\n",
            "text/markdown",
        )
        manifest = {
            "schema": SCHEMA,
            "version": "v1",
            "release_status": "draft",
            "categories": [{"id": k, "display_name": v} for k, v in LABELS.items()],
            "live_model_ids": sorted(self.models),
            "cases": self.cases,
            "objects": list(self.objects.values()),
            "qualification": {"state": "pending", "receipts": []},
        }
        (self.root / "manifest.json").write_bytes(json_bytes(manifest))
        print(
            json.dumps(
                {
                    "root": str(self.root),
                    "cases": len(self.cases),
                    "objects": len(self.objects),
                    "bytes": sum(o["size_bytes"] for o in self.objects.values()),
                    "release_status": "draft",
                }
            )
        )


def proteins(pack):
    from Bio.PDB import PDBParser
    from Bio.SeqUtils import seq1

    entries = [
        "1UBQ",
        "1CRN",
        "1PGA",
        "1L2Y",
        "1VII",
        "1BDD",
        "2GB1",
        "1BPI",
        "1LYZ",
        "2TRX",
    ]
    structures = []
    for index, pdb in enumerate(entries):
        data = pack.fetch(f"https://files.rcsb.org/download/{pdb}.pdb", pdb + ".pdb")
        structure = PDBParser(QUIET=True).get_structure(pdb, io.StringIO(data.decode()))
        chain = next(next(structure.get_models()).get_chains())
        residues = [r for r in chain if r.id[0] == " " and "CA" in r]
        sequence = "".join(seq1(r.resname) for r in residues)
        assert 8 <= len(sequence) <= 500 and set(sequence) <= set(
            "ACDEFGHIKLMNPQRSTVWY"
        )
        # First model and first protein chain only, with nonprotein/PII metadata removed.
        from Bio.PDB import PDBIO, Select

        class ProteinOnly(Select):
            def accept_model(self, model):
                return model.id == 0

            def accept_chain(self, value):
                return value.id == chain.id

            def accept_residue(self, value):
                return value.id[0] == " "

        writer, output = PDBIO(), io.StringIO()
        writer.set_structure(structure)
        writer.save(output, ProteinOnly())
        provenance = {
            "source": f"https://www.rcsb.org/structure/{pdb}",
            "license": "CC0-1.0",
            "license_url": PDB_POLICY,
            "attribution": f"wwPDB deposit {pdb}; see linked experimental authors",
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "transformation": "first model, first protein chain; sequence from resolved standard residues; metadata removed",
        }
        fasta = pack.add(
            f"assets/proteins/{pdb}.fasta",
            f">{pdb}|chain={chain.id}|resolved-residues\n{sequence}\n",
            "text/x-fasta",
            provenance=provenance,
        )
        coordinate = pack.add(
            f"assets/proteins/{pdb}.pdb",
            output.getvalue(),
            "chemical/x-pdb",
            provenance=provenance,
        )
        structures.append((pdb, sequence, fasta, coordinate))
        pack.case(
            "sequence-search",
            pdb.lower(),
            f"{pdb}: find structural homologs",
            "Search a distinct deposited protein against PDB70. No assertion that a returned hit proves function.",
            [
                pack.recipe(
                    "msa-search-pdb70",
                    {
                        "sequence": sequence,
                        "databases": ["pdb70_220313"],
                        "output_alignment_formats": ["a3m"],
                    },
                )
            ],
            {
                "kind": "alignment",
                "description": "A parseable alignment/search result preserving the query sequence; zero homologs is allowed.",
            },
            assets=[fasta],
        )
        if index < 5:
            pack.case(
                "structure",
                pdb.lower(),
                f"{pdb}: single-chain structure",
                "Predict a compact protein structure from a real deposited protein sequence, then compare coverage with its reference coordinates.",
                [
                    pack.recipe(
                        "openfold2",
                        {
                            "input_id": "starter-" + pdb,
                            "sequence": sequence,
                            "selected_models": [1],
                            "relax_prediction": False,
                        },
                    )
                ],
                {
                    "kind": "structure",
                    "description": "Finite protein coordinates, nonzero residues and expected sequence coverage; not exact agreement with the experimental structure.",
                },
                assets=[fasta, coordinate],
            )
        if index < 4:
            pack.case(
                "protein-design",
                "sequence-" + pdb.lower(),
                f"{pdb}: backbone-conditioned sequence",
                "Design one sequence for a distinct compact backbone. This is a geometry/sequence-format example, not a claim of experimental folding or binding.",
                [
                    pack.recipe(
                        "proteinmpnn",
                        {
                            "input_pdb": file_ref(
                                coordinate, media_type="chemical/x-pdb"
                            ),
                            "num_seq_per_target": 1,
                            "random_seed": 20260920 + index,
                        },
                    )
                ],
                {
                    "kind": "sequence",
                    "description": "At least one protein sequence with valid amino acids and length consistent with the backbone.",
                },
                assets=[coordinate, fasta],
            )
    return structures


def molecules_and_genomics(pack):
    molecules = [
        ("aspirin", "CC(=O)OC1=CC=CC=C1C(=O)O"),
        ("caffeine", "Cn1c(=O)c2c(ncn2C)n(C)c1=O"),
        ("paracetamol", "CC(=O)Nc1ccc(O)cc1"),
        ("ibuprofen", "CC(C)Cc1ccc(C(C)C(=O)O)cc1"),
        ("vanillin", "COc1cc(C=O)ccc1O"),
        ("salicylic-acid", "O=C(O)c1ccccc1O"),
        ("nicotinamide", "NC(=O)c1cccnc1"),
        ("menthol", "CC(C)C1CCC(C)CC1O"),
        ("theobromine", "Cn1cnc2c1c(=O)[nH]c(=O)n2C"),
        ("catechol", "Oc1ccccc1O"),
    ]
    for name, smiles in molecules:
        path = pack.add(
            f"small-molecule/{name}/molecule.smi",
            smiles + " " + name + "\n",
            "chemical/x-daylight-smiles",
        )
        payload = {
            "smi": smiles,
            "num_molecules": 1,
            "particles": 16,
            "iterations": 8,
            "radius": 1.5,
            "min_similarity": 0.3,
        }
        recipes = [pack.recipe("molmim", payload)]
        if name in {"aspirin", "caffeine"}:
            recipes.append(
                pack.recipe(
                    "genmol",
                    {
                        "smiles": "[*{10-15}]",
                        "num_molecules": 1,
                        "temperature": 1,
                        "noise": 0,
                    },
                )
            )
        pack.case(
            "small-molecule",
            name,
            name.replace("-", " ").title(),
            "A benign named molecule illustrating a distinct chemical scaffold. Generated analogues have no asserted therapeutic activity or safety.",
            recipes,
            {
                "kind": "molecule",
                "description": "Parseable nonempty SMILES/SDF output; compare identity and molecular format, not biological efficacy.",
            },
            assets=[path],
        )
    patterns = [
        ("balanced", 0.5, 64),
        ("at-rich", 0.2, 80),
        ("gc-rich", 0.8, 80),
        ("short-context", 0.5, 24),
        ("longer-context", 0.5, 256),
        ("gc-transition", 0.7, 192),
        ("low-complexity", 0.25, 96),
        ("alternating", 0.5, 128),
        ("homopolymer-flank", 0.5, 160),
        ("mixed-composition", 0.4, 320),
    ]
    for index, (name, gc, length) in enumerate(patterns):
        rng = random.Random(20260920 + index)
        sequence = "".join(
            rng.choice("GC" if rng.random() < gc else "AT") for _ in range(length)
        )
        if name == "gc-transition":
            sequence = "AT" * 32 + sequence[64:]
        if name == "low-complexity":
            sequence = "AATAT" * 19 + "A"
        if name == "alternating":
            sequence = "ACGT" * 32
        if name == "homopolymer-flank":
            sequence = "A" * 24 + sequence[24:-24] + "T" * 24
        path = pack.add(
            f"genomics/{name}/synthetic-dna.fasta",
            f">synthetic-{name}|no-known-biological-function\n{sequence}\n",
            "text/x-fasta",
        )
        pack.case(
            "genomics",
            name,
            "Synthetic DNA: " + name,
            "Artificial sequence-composition fixture, not a natural gene, pathogen sequence, expression construct or functional design.",
            [
                pack.recipe(
                    "evo2-40b",
                    {
                        "sequence": sequence,
                        "num_tokens": 16,
                        "temperature": 0.7,
                        "top_k": 1,
                        "top_p": 0,
                        "random_seed": 20260920 + index,
                        "enable_logits": False,
                        "enable_sampled_probs": False,
                        "enable_elapsed_ms_per_token": True,
                    },
                )
            ],
            {
                "kind": "dna",
                "description": "Bounded continuation using DNA letters; no claim of biological function.",
            },
            assets=[path],
        )


def imaging(pack):
    import nibabel as nib

    source = pack.args.cache / "sources/pneumoniamnist_224.npz"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest() == pack.source_lock[source.name]
    )
    assert (
        hashlib.md5(source.read_bytes()).hexdigest()
        == "d6a3c71de1b945ea11211b03746c1fe1"
    )
    with np.load(source, allow_pickle=False) as dataset:
        for index in range(5):
            pixels = dataset["test_images"][index]
            assert pixels.shape == (224, 224) and pixels.dtype == np.uint8
            output = io.BytesIO()
            Image.fromarray(pixels).save(
                output, format="PNG"
            )  # Fresh PNG, no source metadata.
            provenance = {
                "source": "https://zenodo.org/records/10519652/files/pneumoniamnist_224.npz",
                "license": "CC-BY-4.0",
                "license_url": "https://medmnist.com/",
                "attribution": "Yang et al., MedMNIST v2, Scientific Data 10:41 (2023); Kermany et al., Cell 172:1122-1131 (2018)",
                "transformation": f"test_images[{index}], 224px grayscale exported to PNG with no metadata; dataset label retained separately",
                "source_md5": "d6a3c71de1b945ea11211b03746c1fe1",
            }
            path = pack.add(
                f"imaging/chest-{index + 1}/image.png",
                output.getvalue(),
                "image/png",
                provenance=provenance,
            )
            label = pack.add(
                f"imaging/chest-{index + 1}/reference.json",
                {
                    "dataset_index": index,
                    "dataset_label": int(dataset["test_labels"][index, 0]),
                    "label_names": {"0": "normal", "1": "pneumonia"},
                    "clinical_ground_truth_claim": False,
                    "limitation": "Small preprocessed pediatric teaching image; not suitable for clinical deployment validation.",
                },
                provenance=provenance,
            )
            pack.case(
                "imaging",
                f"chest-{index + 1}",
                f"Public pediatric X-ray {index + 1}",
                "A distinct de-identified public chest image, for file transport and image-conditioned response testing only. The upstream dataset is expressly not intended for clinical use.",
                [
                    pack.recipe(
                        "nv-reason-cxr-3b",
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": file_ref(
                                                    path,
                                                    encoding="data-url",
                                                    media_type="image/png",
                                                )
                                            },
                                        },
                                        {
                                            "type": "text",
                                            "text": "Find abnormalities and support devices.",
                                        },
                                    ],
                                }
                            ],
                            "max_completion_tokens": 4096,
                            "temperature": 0,
                        },
                        protocol="openai-chat",
                    )
                ],
                {
                    "kind": "chat",
                    "description": "A nonempty image-conditioned description; no diagnostic-accuracy claim.",
                },
                assets=[path, label],
            )
    for index, name in enumerate(
        ["single-ellipsoid", "paired-ellipsoids", "elongated-ellipsoid"]
    ):
        grid = np.indices((64, 64, 64))
        center = np.array([32, 32, 32])[:, None, None, None]
        axes = np.array([16, 20, 24] if index != 2 else [10, 12, 26])[
            :, None, None, None
        ]
        shape = (((grid - center) / axes) ** 2).sum(axis=0) <= 1
        if index == 1:
            shape |= (
                ((grid - np.array([18, 22, 24])[:, None, None, None]) / 8) ** 2
            ).sum(axis=0) <= 1
        volume = np.where(shape, 60, -1000).astype(np.float32)
        image = nib.Nifti1Image(volume, np.diag([2, 2, 2, 1]))
        data = image.to_bytes()
        assert nib.Nifti1Image.from_bytes(data).shape == (64, 64, 64)
        # The deployed VISTA adapter localizes to .nii.gz. Use its qualified
        # gzip path rather than the schema's wider raw-NIfTI alternative.
        path = pack.add(
            f"imaging/{name}/synthetic.nii.gz",
            gzip.compress(data, mtime=0),
            "application/gzip",
        )
        pack.case(
            "imaging",
            name,
            "Synthetic CT: " + name,
            "An artificial intensity phantom, not anatomy. Exercises volume decoding, shape and finite segmentation outputs, not organ accuracy.",
            [
                pack.recipe(
                    "nv-segment-ct",
                    {
                        "input_nifti_base64": file_ref(
                            path, media_type="application/gzip"
                        ),
                        "label_prompt": [1],
                    },
                )
            ],
            {
                "kind": "segmentation-volume",
                "description": "A decodable integer-label volume with the input dimensions; an empty organ mask is valid for a non-anatomical phantom.",
            },
            assets=[path],
        )
    for index, name in enumerate(["sparse-cells", "crowded-cells"]):
        image = Image.new("L", (256, 256), 12)
        draw = ImageDraw.Draw(image)
        rng = random.Random(20260920 + index)
        centers = []
        for _ in range(16 if index == 0 else 48):
            x, y, r = rng.randint(15, 240), rng.randint(15, 240), rng.randint(5, 11)
            centers.append((x, y))
            draw.ellipse((x - r, y - r, x + r, y + r), fill=rng.randint(160, 245))
        output = io.BytesIO()
        image.save(output, format="PNG")
        path = pack.add(f"imaging/{name}/synthetic.png", output.getvalue(), "image/png")
        pack.case(
            "imaging",
            name,
            "Synthetic microscopy: " + name,
            "Distinct sparse and crowded cell-like spot fields; synthetic overlap is not a reference segmentation annotation.",
            [
                pack.recipe(
                    "cellpose-cpsam-v2",
                    {
                        "image_base64": file_ref(path, media_type="image/png"),
                        "media_type": "image/png",
                        "diameter": None,
                        "research_only": True,
                    },
                ),
                pack.recipe(
                    "sam2-1-hiera-large",
                    {
                        "mode": "prompted-image",
                        "media_base64": file_ref(path, media_type="image/png"),
                        "media_type": "image/png",
                        "points": [
                            {
                                "x": centers[0][0],
                                "y": centers[0][1],
                                "label": 1,
                                "object_id": 1,
                            }
                        ],
                    },
                ),
            ],
            {
                "kind": "segmentation-image",
                "description": "A decodable 256×256 label mask and finite summary values.",
            },
            assets=[path],
        )


def single_cell(pack):
    import anndata as ad
    import pandas as pd

    scenarios = [
        ("balanced", 160, 96, 0.0),
        ("batch-shift", 160, 96, 1.5),
        ("small-study", 80, 64, 0.3),
        ("more-cells", 256, 96, 0.5),
        ("sparse-counts", 160, 96, 0.4),
        ("rare-population", 192, 96, 0.7),
        ("three-batches", 192, 96, 0.5),
        ("wider-gene-panel", 160, 160, 0.5),
        ("partial-labels", 160, 96, 0.5),
        ("imbalanced-labels", 160, 96, 0.5),
    ]
    for index, (name, cells, genes, shift) in enumerate(scenarios):
        rng = np.random.default_rng(20260920 + index)
        batches = np.array(
            [
                "batch-" + str(i % (3 if name == "three-batches" else 2))
                for i in range(cells)
            ]
        )
        labels = np.array(
            [("T-cell", "B-cell", "Unknown", "Monocyte")[i % 4] for i in range(cells)]
        )
        if name == "rare-population":
            labels[4:] = "T-cell"
        if name == "partial-labels":
            labels[::2] = "Unknown"
        if name == "imbalanced-labels":
            labels[:100] = "B-cell"
        means = np.full((cells, genes), 0.2 if name == "sparse-counts" else 1.4)
        means[labels == "T-cell", :12] += 2
        means[labels == "B-cell", 12:24] += 2
        means[labels == "Monocyte", 24:36] += 2
        means[batches == "batch-1", 36:50] += shift
        counts = rng.poisson(means).astype(np.float32)
        relative = f"single-cell/{name}/synthetic.h5ad"
        target = pack.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        obs = pd.DataFrame(
            {"batch": pd.Categorical(batches), "cell_type": pd.Categorical(labels)},
            index=[f"demo-{i:04}" for i in range(cells)],
        )
        ad.settings.allow_write_nullable_strings = True
        ad.AnnData(
            X=counts,
            obs=obs,
            var=pd.DataFrame(index=[f"gene-{i:04}" for i in range(genes)]),
        ).write_h5ad(target, compression="gzip")
        data = target.read_bytes()
        reread = ad.read_h5ad(target)
        assert reread.shape == (cells, genes) and np.all(reread.X >= 0)
        path = pack.add(relative, data, "application/x-hdf5")
        method = "scanvi" if index >= 8 else "scvi"
        payload = {
            "anndata_base64": file_ref(path, media_type="application/x-hdf5"),
            "filename": "synthetic.h5ad",
            "method": method,
            "batch_key": "batch",
            "max_epochs": 2,
            "n_latent": 10,
            "seed": 20260920 + index,
            "research_only": True,
        }
        if method == "scanvi":
            payload.update(labels_key="cell_type", unlabeled_category="Unknown")
        pack.case(
            "single-cell",
            name,
            "Synthetic counts: " + name,
            f"A distinct {cells}-cell × {genes}-gene integer-count simulation. Two epochs keep this onboarding run small; latent-space or label accuracy is not benchmarked.",
            [pack.recipe("scvi-scanvi", payload)],
            {
                "kind": "single-cell",
                "description": f"Finite {cells}×10 latent representation, unchanged cell identities, and an output manifest confirming the selected scVI/scANVI method. The deployed runtime exports embeddings and a trained model, not predicted cell labels.",
            },
            assets=[path],
        )


def aging(pack):
    original = pack.args.altumage_fixture.read_bytes()
    assert (
        hashlib.sha256(original).hexdigest()
        == "5be74e8ed2cbbcd6d3968ac779f6c05fb6ec6dd30c14f5ddfa875e0271c1a8b5"
    )
    fixture = json.loads(original)
    provenance = {
        "source": "https://github.com/rsinghlab/AltumAge/tree/696c477dac9b7641bf283c48af1cc9bb0a0803a3",
        "license": "MIT",
        "attribution": "Lucas Paulo de Lima Camillo / Singh Lab, AltumAge; synthetic fixture by Nebius Scientific AI",
        "transformation": "ordered published CpG IDs and preprocessing center only; synthetic beta perturbations, no person-level methylation data",
    }
    license_text = pack.fetch(
        "https://raw.githubusercontent.com/rsinghlab/AltumAge/696c477dac9b7641bf283c48af1cc9bb0a0803a3/LICENSE",
        "altumage-LICENSE",
    )
    pack.add(
        "licenses/AltumAge-MIT.txt", license_text, "text/plain", provenance=provenance
    )
    cpgs = pack.add(
        "assets/aging/altumage-cpgs.json", fixture["cpg_sites"], provenance=provenance
    )
    center = np.asarray(fixture["samples"][0]["beta_values"])
    for index, name in enumerate(
        [
            "center",
            "higher-methylation",
            "lower-methylation",
            "sparse-perturbation",
            "mixed-perturbation",
        ]
    ):
        beta = center.copy()
        if index == 1:
            beta += 0.02
        if index == 2:
            beta -= 0.02
        if index == 3:
            beta[::11] += 0.05
        if index == 4:
            beta += np.random.default_rng(20260924).normal(0, 0.02, len(beta))
        values = pack.add(
            f"age-prediction/altumage-{name}/beta.json",
            np.clip(beta, 0, 1).tolist(),
            provenance=provenance,
        )
        pack.case(
            "age-prediction",
            "altumage-" + name,
            "Synthetic methylation: " + name,
            "A full 20,318-CpG, non-personal synthetic feature vector. Perturbations demonstrate input handling, not an intervention or an expected age direction.",
            [
                pack.recipe(
                    "altumage",
                    {
                        "cpg_sites": file_ref(cpgs, media_type="application/json"),
                        "missing_values": "error",
                        "samples": [
                            {
                                "sample_id": name,
                                "beta_values": file_ref(
                                    values, media_type="application/json"
                                ),
                            }
                        ],
                    },
                )
            ],
            {
                "kind": "aging",
                "description": "One finite age estimate tied to the synthetic sample ID and the exact 20,318-feature input.",
            },
            assets=[cpgs, values],
        )
    profiles = [
        ("baseline-30", 30, 45, 0.1, 30),
        ("baseline-60", 60, 45, 0.1, 30),
        ("inflammation", 50, 40, 1.2, 22),
        ("lower-albumin", 50, 35, 0.1, 30),
        ("changed-cell-fractions", 50, 45, 0.1, 18),
    ]
    for name, age, albumin, crp, lymphocytes in profiles:
        value = {
            "sample_id": name,
            "age_years": age,
            "albumin_g_l": albumin,
            "creatinine_umol_l": 80,
            "glucose_mmol_l": 5,
            "c_reactive_protein_mg_dl": crp,
            "lymphocyte_percent": lymphocytes,
            "mean_cell_volume_fl": 90,
            "red_cell_distribution_width_percent": 13,
            "alkaline_phosphatase_u_l": 70,
            "white_blood_cell_count_10e3_per_ul": 6,
        }
        path = pack.add(
            f"age-prediction/phenoage-{name}/labs.json", {"samples": [value]}
        )
        pack.case(
            "age-prediction",
            "phenoage-" + name,
            "Fictional blood markers: " + name,
            "A synthetic profile with explicit units. These numbers do not describe a patient and must not be used to infer treatment or individual risk.",
            [pack.recipe("phenoage", {"samples": [value]})],
            {
                "kind": "aging",
                "description": "A finite formula result with matching sample identity; reported units must match the request.",
            },
            assets=[path],
        )


def speech(pack):
    demo = pack.args.demo_assets
    recordings = [
        (
            "en-gastroenteritis",
            "en",
            "day1_consultation01_conversation.wav",
            "day1_consultation01",
        ),
        (
            "en-eczema",
            "en",
            "day1_consultation02_conversation.wav",
            "day1_consultation02",
        ),
        ("de-herzrasen", "de", "hhu-herzrasen.wav", "5b4e0fb9a15939a76a025c06d3fd505f"),
        (
            "de-infekt",
            "de",
            "hhu-grippaler-infekt.wav",
            "1405493ccdbe3188d779c4e2d0bebb37",
        ),
        (
            "de-polyarthritis",
            "de",
            "hhu-polyarthritis.wav",
            "5fa45b3f9155318d841e4af94dc07398",
        ),
    ]
    checksums = {
        line.split()[1].removeprefix("./"): line.split()[0]
        for line in (demo / "SHA256SUMS").read_text().splitlines()
    }
    for slug, language, filename, source_id in recordings:
        source_path = f"ready/{language}/{filename}"
        data = (demo / source_path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == checksums[source_path]
        with wave.open(io.BytesIO(data)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (
                1,
                2,
                16000,
            )
            duration = wav.getnframes() / wav.getframerate()
            original_pcm = wav.readframes(wav.getnframes())
        # Preserve the full recording while fitting the public gateway's
        # bounded inline transfer. FLAC is lossless and explicitly supported
        # by the live speech contracts. Verify decoded samples, not just size.
        data = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-map_metadata",
                "-1",
                "-c:a",
                "flac",
                "-f",
                "flac",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=True,
        ).stdout
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=True,
        ).stdout
        assert decoded == original_pcm and len(data) <= 16 * 1024 * 1024
        hhu_slugs = {
            "de-herzrasen": "good-practice-wwsz-technik",
            "de-infekt": "good-practice-wwsz-technik-und-anamnese-bei-grippalem-infekt",
            "de-polyarthritis": "good-practice-schlechte-nachrichten-berbringen-spikes-modell",
        }
        source = (
            "https://github.com/babylonhealth/primock57/tree/cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5"
            if language == "en"
            else "https://media.hhu.de/video/" + hhu_slugs[slug] + "/" + source_id
        )
        provenance = {
            "source": source,
            "license": "CC-BY-4.0" if language == "en" else "CC-BY-3.0-DE",
            "attribution": "Babylon Health / PriMock57 role-play participants"
            if language == "en"
            else "Heinrich-Heine-Universität Düsseldorf; Olaf Reddemann, Christian Cujovic, Marlon Jarek"
            if slug != "de-polyarthritis"
            else "HHU; Prof. Jürgen in der Schmitten, Susan Fararuni, Marlon Jarek",
            "transformation": "Full acted consultation; English synchronized tracks mixed at 0.5 gain; German audio extracted from teaching MP4; 16kHz mono PCM16 losslessly encoded to FLAC, no cuts; decoded samples verified byte-identical to approved demo WAV",
        }
        path = pack.add(
            f"speech/{slug}/conversation.flac",
            data,
            "audio/flac",
            provenance=provenance,
        )
        recipes = [
            pack.recipe(
                "nemotron-speech-multilingual-0-6b",
                {
                    "audio": file_ref(path, media_type="audio/flac"),
                    "options": {
                        "model": "nemotron-speech-multilingual-0.6b",
                        "language": language,
                    },
                },
            )
        ]
        if language == "en":
            recipes += [
                pack.recipe(
                    "nemotron-speech-en-0-6b",
                    {
                        "audio": file_ref(path, media_type="audio/flac"),
                        "options": {
                            "model": "nemotron-speech-en-0.6b",
                            "language": "en",
                        },
                    },
                ),
                pack.recipe(
                    "parakeet-realtime-eou-120m-v1",
                    {"audio": file_ref(path, media_type="audio/flac")},
                ),
                pack.recipe(
                    "diar-streaming-sortformer-4spk-v2-1",
                    {"audio": file_ref(path, media_type="audio/flac")},
                ),
            ]
            for suffix in ["_doctor.TextGrid", "_patient.TextGrid", ".json"]:
                pack.add(
                    f"speech/{slug}/references/{source_id}{suffix}",
                    (demo / "references/en" / (source_id + suffix)).read_bytes(),
                    "text/plain" if suffix.endswith("TextGrid") else "application/json",
                    provenance=provenance,
                    models=[r["model_id"] for r in recipes],
                )
        pack.case(
            "speech",
            slug,
            "Acted consultation: " + slug,
            f"Full {duration:.2f}-second approved public teaching recording. Speaker overlap and pauses are retained; not a real customer/patient recording.",
            recipes,
            {
                "kind": "speech",
                "description": "Nonempty transcript with bounded ordered timestamps, or speaker segments for diarization; compare human English TextGrid references separately.",
            },
            assets=[path],
            reference_note="German audio includes teaching narration and has no verified human transcript here. Do not turn narrator explanations into patient findings."
            if language == "de"
            else "Original human TextGrid transcripts and clinician-authored notes are references, not model predictions.",
        )
    prompts = [
        (
            "en-appointment",
            "en",
            "This is a fictional clinic demonstration. Your appointment is on Tuesday at ten in the morning.",
        ),
        (
            "de-appointment",
            "de",
            "Dies ist eine fiktive Demonstration. Ihr Termin ist am Dienstag um zehn Uhr vormittags.",
        ),
        (
            "en-lab-workflow",
            "en",
            "Label each demonstration tube, record its identifier, and place it in the blue rack.",
        ),
        (
            "de-interview",
            "de",
            "Seit wann bestehen die Beschwerden, und welche Fragen möchten Sie heute besprechen?",
        ),
        (
            "en-accessibility",
            "en",
            "The upload has finished. Your analysis is queued. You may close this page and return later.",
        ),
    ]
    for slug, language, text in prompts:
        path = pack.add(f"speech/{slug}/text.txt", text + "\n", "text/plain")
        pack.case(
            "speech",
            slug,
            "Speech synthesis: " + slug,
            "An original fictional text prompt for speech synthesis; no personal data or patient instructions.",
            [
                pack.recipe(
                    "magpie-tts-multilingual-357m",
                    {"text": text, "language": language, "voice": "Sofia"},
                )
            ],
            {
                "kind": "audio",
                "description": "Decodable nonempty audio with finite duration; compare spoken wording with the exact source text.",
            },
            assets=[path],
        )


def general_ai(pack):
    prompts = [
        (
            "explain-fasta",
            "Explain the FASTA format in three short bullet points. Do not invent a sequence.",
        ),
        (
            "summarize-status",
            "Summarize this fictional experiment log: three jobs completed, one queued, no failed jobs.",
        ),
        (
            "extract-labels",
            "Return a JSON array with the sample labels from: DEMO-A, DEMO-B, DEMO-C.",
        ),
        (
            "translate-workflow",
            "Translate to German: Upload the example file and wait for the result.",
        ),
        (
            "compare-units",
            "Explain the distinction between milliseconds and seconds using 1500 milliseconds as the example.",
        ),
    ]
    for slug, prompt in prompts:
        path = pack.add(f"general-ai/{slug}/prompt.txt", prompt + "\n", "text/plain")
        pack.case(
            "general-ai",
            slug,
            slug.replace("-", " ").title(),
            "A bounded general-purpose text task with an inspectable input.",
            [
                pack.recipe(
                    "qwen3-8b",
                    {
                        "messages": [
                            {"role": "user", "content": prompt + " /no_think"}
                        ],
                        "max_completion_tokens": 1024,
                        "temperature": 0.2,
                    },
                    protocol="openai-chat",
                )
            ],
            {
                "kind": "chat",
                "description": "A nonempty final answer addressing the supplied task; reasoning-only or truncated responses do not pass.",
            },
            assets=[path],
        )
    scenes = [
        (
            "glassware",
            "A clean laboratory bench with empty glass beakers, soft daylight, no writing or logos",
        ),
        (
            "helix-art",
            "An abstract teal double helix sculpture on a pale green background, scientific art, no text",
        ),
        (
            "robotics-room",
            "A small tabletop robot in a bright research room, no people, no logos",
        ),
        ("forest", "A quiet forest stream in morning light, watercolor illustration"),
        (
            "geometric-poster",
            "A minimalist scientific poster background of circles and hexagons, teal and cream, no text",
        ),
    ]
    for index, (slug, prompt) in enumerate(scenes):
        path = pack.add(f"general-ai/{slug}/prompt.txt", prompt + "\n", "text/plain")
        pack.case(
            "general-ai",
            slug,
            "Image generation: " + slug,
            "An original nonmedical illustration prompt. Generated images are not measured scientific data.",
            [
                pack.recipe(
                    "sdxl",
                    {
                        "prompt": prompt,
                        "seed": 20260920 + index,
                        "steps": 20,
                        "response_format": "b64_json",
                    },
                )
            ],
            {
                "kind": "image",
                "description": "A decodable nonempty image with valid dimensions; no exact pixel equality requirement.",
            },
            assets=[path],
        )


def scientific_manifest(pack, identifier, model, request, entries):
    template = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "starter." + identifier.replace("/", "."),
        "entries": entries,
    }
    path = pack.add(
        identifier + "/input-manifest.template.json", template, models=[model]
    )
    request = copy.deepcopy(request)
    request["input_manifest"] = {"$manifest": path}
    request["client_context"] = {
        "batch_id": "starter." + identifier.replace("/", "."),
        "correlation_id": "starter.v1",
        "display_name": identifier,
    }
    return pack.recipe(model, request, protocol="scientific-batch-v1"), path


def scientific_structures(pack, structures):
    assignments = [
        ("esmfold2", structures[5]),
        ("esmfold2-fast", structures[6]),
        ("protenix-v2", structures[7]),
        ("alphafold3", structures[8]),
        ("openfold3-openbind", structures[9]),
    ]
    for model, (pdb, sequence, fasta, coordinate) in assignments:
        identifier = f"structure/{pdb.lower()}"
        request = copy.deepcopy(pack.contracts[model][0]["examples"][0])
        if model.startswith("esmfold2"):
            payload = {
                "sequences": [{"id": "A", "type": "protein", "sequence": sequence}]
            }
            entry = pack.input_contracts[model]["entry"]
            name, semantic = entry["name"], entry["semantic_type"]
            request["parameters"]["sequence"] = sequence
        elif model == "protenix-v2":
            payload = [
                {
                    "name": "starter-" + pdb,
                    "sequences": [{"proteinChain": {"count": 1, "sequence": sequence}}],
                }
            ]
            name, semantic = "protenix-input", "protenix-input-json/v1"
        elif model == "alphafold3":
            payload = {
                "dialect": "alphafold3",
                "version": 2,
                "name": "starter-" + pdb,
                "modelSeeds": [20260920],
                "sequences": [
                    {
                        "protein": {
                            "id": "A",
                            "sequence": sequence,
                            "unpairedMsa": ">query\n" + sequence + "\n",
                            "pairedMsa": "",
                            "templates": [],
                        }
                    }
                ],
            }
            name, semantic = "fold-input", "alphafold3-fold-input/v1"
        else:
            payload = {
                "queries": {
                    "starter-" + pdb: {
                        "chains": [
                            {
                                "chain_ids": ["A"],
                                "molecule_type": "protein",
                                "sequence": sequence,
                            }
                        ]
                    }
                }
            }
            name, semantic = "openfold3-input", "openfold3-input-json/v1"
        path = pack.add(
            identifier + "/model-input.json",
            payload,
            provenance=pack.objects[fasta]["provenance"],
            models=[model],
        )
        recipe, manifest = scientific_manifest(
            pack,
            identifier,
            model,
            request,
            [
                {
                    "name": name,
                    "semantic_type": semantic,
                    "artifact": file_ref(path, media_type="application/json"),
                },
            ],
        )
        recipes = [recipe]
        if model == "esmfold2":
            recipes.append(
                pack.recipe(
                    "boltz2",
                    {
                        "polymers": [
                            {
                                "id": "A",
                                "molecule_type": "protein",
                                "sequence": sequence,
                                "msa": {
                                    "msa_search": {
                                        "a3m": {
                                            "alignment": ">query\n" + sequence + "\n"
                                        }
                                    }
                                },
                            }
                        ]
                    },
                )
            )
        if model == "openfold3-openbind":
            recipes.append(
                pack.recipe(
                    "openfold3",
                    {
                        "request_id": "starter-" + pdb,
                        "inputs": [
                            {
                                "input_id": pdb,
                                "output_format": "cif",
                                "molecules": [
                                    {"id": "A", "type": "protein", "sequence": sequence}
                                ],
                            }
                        ],
                    },
                )
            )
        if model == "alphafold3":
            recipes.append(
                pack.recipe(
                    "diffdock",
                    {
                        "protein": file_ref(coordinate, media_type="chemical/x-pdb"),
                        "ligand": "CC(=O)OC1=CC=CC=C1C(=O)O",
                        "ligand_file_type": "txt",
                    },
                )
            )
        pack.case(
            "structure",
            pdb.lower(),
            f"{pdb}: {model} structure workflow",
            "Predict a distinct deposited protein through the durable scientific API. Any optional aspirin docking is a format/pose exercise, not evidence of affinity or a proposed therapeutic target.",
            recipes,
            {
                "kind": "structure",
                "description": "Successful semantic result and downloadable, finite, nonempty protein coordinates; docking additionally requires valid ligand poses.",
            },
            assets=[fasta, coordinate, path, manifest],
        )


def archive_bytes(members):
    target = io.BytesIO()
    with gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                archive.addfile(info, io.BytesIO(data))
    return target.getvalue()


def protein_design(pack):
    root = Path(__file__).resolve().parents[1]
    base = root / "models/cancer-immunotherapy"
    fragments = {
        name: base / "runtime-images" / name / "activation/fragment.json"
        for name in ["boltzgen", "mosaic", "proteina-complexa", "rfdiffusion"]
    }
    fragments["bindcraft"] = base / "images/bindcraft-native/activation/fragment.json"
    for model, fragment_path in fragments.items():
        fragment = json.loads(fragment_path.read_bytes())
        fixtures = fragment["public_fixtures"]
        request = json.loads((root / fixtures["request"]).read_bytes())
        declarations = fixtures["supporting_inputs"]
        template_path = next(
            d["path"] for d in declarations if d["role"] == "request-input-manifest"
        )
        original_manifest = json.loads((root / template_path).read_bytes())
        assets, entries = [], []
        identifier = "protein-design/" + model
        provenance = {
            "source": "https://github.com/rene-tech/nebius-solutions-library/tree/78f40fe81e2c51acdfd924de94834607d4c3ddc7/k8s-inference/"
            + str(fragment_path.relative_to(root)),
            "license": "Apache-2.0",
            "attribution": "Nebius Scientific AI qualification fixtures; public wwPDB structural data (CC0) where present",
            "transformation": "same source fixture bytes; deterministic archive packaging; no customer run output",
        }
        for declaration in declarations:
            if declaration["role"] != "manifest-artifact":
                continue
            source = root / declaration["path"]
            data = source.read_bytes()
            encoding = declaration["encoding"]
            if encoding == "deterministic-tar-gzip-v1":
                data = archive_bytes([(declaration["archive_path"], data)])
            elif encoding == "deterministic-tar-gzip-manifest-v1":
                data = archive_bytes(
                    [
                        (row["archive_path"], (root / row["source_path"]).read_bytes())
                        for row in json.loads(data)["members"]
                    ]
                )
            else:
                assert encoding == "raw"
            # Bind by declared name, or the source's pinned digest, never by order alone.
            matches = [
                entry
                for entry in original_manifest["entries"]
                if entry["name"] == declaration.get("name")
                or entry["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
            ]
            assert len(matches) == 1, (model, source.name, "fixture binding")
            entry = matches[0]
            media, compression = (
                entry["artifact"]["media_type"],
                entry["artifact"]["compression"],
            )
            path = pack.add(
                identifier
                + "/"
                + ("input.tar.gz" if compression == "gzip" else source.name),
                data,
                media,
                provenance=provenance,
                models=[model],
            )
            assets.append(path)
            entries.append(
                {
                    "name": entry["name"],
                    "semantic_type": entry["semantic_type"],
                    "artifact": file_ref(
                        path, media_type=media, compression=compression
                    ),
                }
            )
        recipe, manifest = scientific_manifest(
            pack, identifier, model, request, entries
        )
        pack.case(
            "protein-design",
            model,
            model + ": bounded design workflow",
            "A small public onboarding design task using the exact upstream/runtime fixture contract. Outputs are generated candidates, not experimentally confirmed binders.",
            [recipe],
            {
                "kind": "design",
                "description": "A completed semantic run with parseable candidate sequences/structures and model-native metrics; no binding-efficacy claim.",
            },
            assets=assets + [manifest],
        )
        if model == "rfdiffusion":
            request["parameters"]["contigs"] = ["96-96"]
            request["parameters"]["seed"] = 60921
            second = "protein-design/rfdiffusion-96"
            recipe, manifest = scientific_manifest(
                pack, second, model, request, entries
            )
            pack.case(
                "protein-design",
                "rfdiffusion-96",
                "RFdiffusion: longer 96-residue backbone",
                "A different target length and topology search space from the 76-residue introductory task, with one generated backbone.",
                [recipe],
                {
                    "kind": "design",
                    "description": "Finite backbone coordinates and the requested 96-residue count.",
                },
                assets=assets + [manifest],
            )


def robotics(pack):
    for index, (slug, scene) in enumerate(
        [
            (
                "pick-and-place",
                "A tabletop robot arm moves a red cube into an empty tray",
            ),
            (
                "conveyor",
                "A small conveyor carries blue blocks past a stationary camera",
            ),
            (
                "mobile-robot",
                "A wheeled laboratory robot drives slowly along an empty marked aisle",
            ),
            (
                "pipette-robot",
                "A stationary laboratory camera observes an automated pipette moving above empty tubes",
            ),
            (
                "gripper",
                "Close-up of a robotic gripper opening and closing above a clean tabletop",
            ),
        ]
    ):
        path = pack.add(
            f"physical-ai-robotics/{slug}/prompt.txt", scene + "\n", "text/plain"
        )
        pack.case(
            "physical-ai-robotics",
            slug,
            "Robot video: " + slug,
            "A distinct original scene prompt. Generated motion is not calibrated robotics telemetry or a physics simulator.",
            [
                pack.recipe(
                    "cosmos3-nano",
                    {
                        "prompt": scene,
                        "size": "448x256",
                        "num_frames": 25,
                        "fps": 24,
                        "num_inference_steps": 30,
                        "seed": 20260920 + index,
                    },
                    tool="cosmos3_nano_text_to_video",
                )
            ],
            {
                "kind": "video",
                "description": "A downloadable MP4 with 25 decodable frames, valid dimensions and positive duration.",
            },
            assets=[path],
        )
    for index, slug in enumerate(["warm-lighting", "cool-lighting"]):
        image = Image.new(
            "RGB", (448, 256), (215, 228, 225) if index == 0 else (205, 215, 235)
        )
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 190, 448, 256), fill=(170, 185, 175))
        draw.rectangle((260, 140, 310, 190), fill=(180, 40, 40))
        draw.line(
            [(70, 190), (115, 80), (225, 110), (260, 145)], fill=(80, 90, 100), width=20
        )
        output = io.BytesIO()
        image.save(output, format="PNG")
        path = pack.add(
            f"physical-ai-robotics/{slug}/robot.png", output.getvalue(), "image/png"
        )
        pack.case(
            "physical-ai-robotics",
            slug,
            "Image-conditioned motion: " + slug,
            "An original synthetic robot drawing with a distinct lighting/camera appearance. It contains no recorded telemetry.",
            [
                pack.recipe(
                    "cosmos3-nano",
                    {
                        "prompt": "The robot gently lifts the cube while preserving the scene geometry.",
                        "input_reference": file_ref(path, media_type="image/png"),
                        "size": "448x256",
                        "num_frames": 25,
                        "fps": 24,
                        "num_inference_steps": 30,
                        "seed": 20260930 + index,
                    },
                    tool="cosmos3_nano_image_to_video",
                )
            ],
            {
                "kind": "video",
                "description": "A downloadable decodable image-conditioned MP4; inspect object identity and motion manually.",
            },
            assets=[path],
        )
    for index, slug in enumerate(["moving-block", "moving-disc"]):
        relative = f"physical-ai-robotics/{slug}/synthetic.mp4"
        target = pack.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        frames = []
        for frame in range(25):
            image = Image.new("RGB", (448, 256), (220, 235, 228))
            draw = ImageDraw.Draw(image)
            x = 70 + frame * 7
            (draw.rectangle if index == 0 else draw.ellipse)(
                (x, 115, x + 50, 165), fill=(180, 60, 50)
            )
            frames.append(image.tobytes())
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                "448x256",
                "-r",
                "24",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                "-map_metadata",
                "-1",
                "-n",
                str(target),
            ],
            input=b"".join(frames),
            check=True,
        )
        path = pack.add(relative, target.read_bytes(), "video/mp4")
        pack.case(
            "physical-ai-robotics",
            slug,
            "Video augmentation: " + slug,
            "A synthetic moving-object clip with known frame count, for appearance augmentation and artifact round trips.",
            [
                pack.recipe(
                    "cosmos3-nano",
                    {
                        "prompt": "Preserve the object and its motion. Change only the lighting to a soft warm tone.",
                        "input_reference": file_ref(path, media_type="video/mp4"),
                        "condition_frame_indexes_vision": [0, 1],
                        "condition_video_keep": "first",
                        "size": "448x256",
                        "num_frames": 25,
                        "fps": 24,
                        "num_inference_steps": 30,
                        "seed": 20260940 + index,
                    },
                    tool="cosmos3_nano_video_to_video",
                )
            ],
            {
                "kind": "video",
                "description": "A distinct downloadable MP4 with valid frames; compare input/output appearance and preserved motion, not exact pixels.",
            },
            assets=[path],
        )
    # The pinned LeRobot reader builds this bundle; no customer dataset is used.
    source = pack.args.cache / "lerobot-synthetic-v3.tar.zst"
    if source.exists():
        path = pack.add(
            "physical-ai-robotics/lerobot-lighting/dataset.tar.zst",
            source.read_bytes(),
            "application/x-tar",
        )
        root = Path(__file__).resolve().parents[1]
        parameters = json.loads(
            (
                root
                / "models/general-media/lerobot-augmentation/fixtures/fixture-request.json"
            ).read_bytes()
        )
        parameters["source"] = {
            "kind": "uploaded-bundle",
            "$merge_file": file_ref(
                path, media_type="application/x-tar", compression="zstd"
            ),
        }
        parameters["variants"] = {"count": 1, "seeds": [20260920]}
        parameters["augmentation"]["dimensions"] = [
            parameters["augmentation"]["dimensions"][0]
        ]
        request = copy.deepcopy(
            pack.contracts["cosmos3-lerobot-augmentation"][0]["examples"][0]
        )
        request["parameters"] = parameters
        source_ref = pack.add(
            "physical-ai-robotics/lerobot-lighting/source-reference.template.json",
            {
                "schema": "fs2-serve.nebius.ai/lerobot-source-reference/v1",
                "source": parameters["source"],
            },
        )
        recipe, manifest = scientific_manifest(
            pack,
            "physical-ai-robotics/lerobot-lighting",
            "cosmos3-lerobot-augmentation",
            request,
            [
                {
                    "name": "lerobot-dataset",
                    "semantic_type": "lerobot-v3-bundle/v1",
                    "artifact": file_ref(
                        path, media_type="application/x-tar", compression="zstd"
                    ),
                },
            ],
        )
        pack.case(
            "physical-ai-robotics",
            "lerobot-lighting",
            "LeRobot dataset lighting augmentation",
            "A synthetic 16-frame, single-camera LeRobot v3 episode with four action/state features. Actions are artificial, not robot measurements.",
            [recipe],
            {
                "kind": "lerobot",
                "description": "A downloadable LeRobot v3 bundle that reopens in the pinned reader; numeric actions, timestamps and episode indices unchanged, selected video frames changed. Generated motion need not remain aligned with recorded actions: review trajectories before policy training.",
            },
            assets=[path, manifest, source_ref],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--demo-assets", type=Path, required=True)
    parser.add_argument("--altumage-fixture", type=Path, required=True)
    args = parser.parse_args()
    pack = Builder(args)
    structures = proteins(pack)
    molecules_and_genomics(pack)
    imaging(pack)
    single_cell(pack)
    aging(pack)
    speech(pack)
    general_ai(pack)
    scientific_structures(pack, structures)
    protein_design(pack)
    robotics(pack)
    pack.finish()


if __name__ == "__main__":
    main()
