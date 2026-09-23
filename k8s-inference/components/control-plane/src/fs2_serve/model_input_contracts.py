"""Source-backed input contracts for the currently hosted runtime adapters.

These describe public *payloads*, not admission envelopes or NVIDIA's larger
NIM interfaces. They do not enable routes. Scientific schemas are composed from
the same loaded catalog validators used by ScientificBatchService; runtime
Pydantic snapshots are checked against their source DTOs in the offline tests.
JSON Schema describes structure; the original runtime still checks biological
content, asset identities and cross-field relationships. No values are dropped.
"""

from __future__ import annotations

import array
import copy
import gzip
import json
import struct
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .registry import OperationalModel
from .scientific_batch.profile_catalog import (
    SCIENTIFIC_REQUEST_SCHEMA,
    ScientificProfileCatalog,
    ScientificWorkloadProfile,
)

Schema = dict[str, Any]
_PROTEIN = "ACDEFGHIKLMNPQRSTVWY"
_SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"
_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GENMOL_MASK_DESCRIPTION = (
    "De-novo mask [*{minimum-maximum}], with 1 <= minimum <= maximum <= 512. "
    "The adapter passes floor((minimum + maximum) / 2) as upstream min_add_len, a minimum number of "
    "SAFE mask tokens; [*{10-20}] therefore reports minimum_mask_tokens=15. "
    "Upstream samples token length from its empirical distribution above that minimum. "
    "Neither endpoint is a heavy-atom bound, and maximum is not a maximum token or molecular-size limit. "
    "Measure and filter heavy-atom counts on returned SMILES separately if required."
)


class InputContractUnavailable(ValueError):  # noqa: N818 - published adapter interface
    """The selected runtime/protocol has no reviewed concrete input contract."""


@dataclass(frozen=True)
class ModelInputContract:
    input_schema: Schema
    examples: tuple[dict[str, Any], ...]
    source_refs: tuple[str, ...]
    model_ref: str
    protocol: str


@lru_cache(maxsize=8)
def _resource(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        files("fs2_serve").joinpath("model_input_schemas", name).read_text(encoding="utf-8")
    )
    return value


def _field(kind: str, description: str, **constraints: Any) -> Schema:
    return {"type": kind, "description": description, **constraints}


def _object(properties: Schema, required: tuple[str, ...], description: str) -> Schema:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
        "description": description,
    }


def _array(items: Schema, description: str, **bounds: Any) -> Schema:
    return _field("array", description, items=items, **bounds)


def _integer(description: str, minimum: int, maximum: int, default: int) -> Schema:
    return _field("integer", description, minimum=minimum, maximum=maximum, default=default)


def _constant(value: Any, description: str) -> Schema:
    return {"const": value, "default": value, "description": description}


def _artifact_reference(*, media_types: tuple[str, ...]) -> Schema:
    """Small immutable pointer returned by the platform upload tools."""

    return _object(
        {
            "artifact_id": _field(
                "string",
                "Caller-owned immutable artifact UUID returned after upload finalization.",
                pattern=_ARTIFACT_ID_PATTERN,
            ),
            "sha256": _field("string", "SHA-256 of the exact uploaded bytes.", pattern=_SHA256_PATTERN),
            "size_bytes": _field("integer", "Exact uploaded byte count.", minimum=0),
            "media_type": _field(
                "string",
                "Media type of the uploaded bytes.",
                enum=list(media_types),
            ),
            "compression": _constant("none", "Artifact input must be stored without transport compression."),
        },
        ("artifact_id", "sha256", "size_bytes", "media_type", "compression"),
        "Tenant-owned artifact reference. Upload and finalize the bytes before submitting this model call.",
    )


def _fixture_reference(*fixture_ids: str) -> Schema:
    return _object(
        {
            "fixture_id": _field(
                "string",
                "Pinned server-side smoke fixture; no fixture bytes pass through the language model.",
                enum=list(fixture_ids),
            )
        },
        ("fixture_id",),
        "Pinned server-side fixture reference.",
    )


def _transportable(
    inline: Schema,
    *,
    materialization: str,
    media_types: tuple[str, ...],
    max_bytes: int,
    fixture_ids: tuple[str, ...] = (),
) -> Schema:
    """Allow bulk bytes to move out-of-band while retaining the inline API."""

    choices = [inline, _artifact_reference(media_types=media_types)]
    if fixture_ids:
        choices.append(_fixture_reference(*fixture_ids))
    return {
        "anyOf": choices,
        "description": (
            inline.get("description", "Model input")
            + " Prefer an artifact reference for nontrivial files so clients never serialize file bytes through an LLM."
        ),
        "x-fs2-artifact-materialization": materialization,
        "x-fs2-artifact-max-bytes": max_bytes,
        "x-fs2-artifact-media-types": list(media_types),
    }


_DESCRIPTIONS = {
    "id": "Caller-chosen protein chain identifier.",
    "polymers": "Protein chains with caller-supplied A3M alignments. This runtime does not accept ligands.",
    "molecule_type": "The portable Boltz2 endpoint supports proteins only.",
    "sequence": "Amino-acid sequence; see this field's alphabet and length constraints.",
    "alignment": "Literal A3M text, including FASTA headers and aligned sequences; not a URL or artifact ID.",
    "format": "Alignment serialization format.",
    "rank": "Accepted for compatibility but NOT consumed by the current Boltz2 adapter.",
    "msa": "Caller-supplied alignment. This endpoint does not perform online MSA search.",
    "msa_search": "Required nesting for the supplied alignment; this name does not trigger an MSA search.",
    "a3m": "A3M alignment object.",
    "recycling_steps": "Number of model recycling iterations.",
    "sampling_steps": "Number of diffusion sampling steps.",
    "diffusion_samples": "Number of diffusion predictions generated by this request.",
    "output_format": "Structure serialization produced by this runtime.",
    "smiles": "De-novo generation mask, e.g. [*{20-30}]; bounds must satisfy 1 <= minimum <= maximum <= 512.",
    "num_molecules": "Requested number of molecules.",
    "scoring": "Molecule scoring function (case-insensitive QED or LogP).",
    "temperature": "Finite positive sampling temperature; numeric strings are also accepted by this adapter.",
    "noise": "Finite nonnegative sampling noise; numeric strings are also accepted by this adapter.",
    "step_size": "Accepted compatibility field; not consumed by the current GenMol de-novo sampler.",
    "unique": "Remove duplicate generated SMILES from the response.",
    "smi": "Starting molecule as a valid SMILES string.",
    "algorithm": "The hosted MolMIM optimizer supports CMA-ES only.",
    "property_name": "The hosted MolMIM optimizer targets QED only.",
    "minimize": "Minimize the target property instead of maximizing it.",
    "min_similarity": "Minimum similarity to the input molecule.",
    "particles": "CMA-ES population size.",
    "iterations": "CMA-ES optimization iterations.",
    "radius": "Latent-space search radius.",
    "databases": "Exact local PDB70 database identity; no alternate or remote databases are accepted.",
    "max_msa_sequences": "Maximum returned alignment sequences.",
    "output_alignment_formats": "Only A3M output is supported.",
    "samples": "Independently named samples. Sample IDs must be unique within the request.",
    "sample_id": "Caller-chosen sample identifier, unique within this request.",
    "age_years": "Chronological age in years.",
    "albumin_g_l": "Serum albumin in g/L.",
    "creatinine_umol_l": "Serum creatinine in micromol/L.",
    "glucose_mmol_l": "Glucose in mmol/L.",
    "lymphocyte_percent": "Lymphocytes as a percentage (0–100), not a fraction.",
    "mean_cell_volume_fl": "Mean corpuscular volume in fL.",
    "red_cell_distribution_width_percent": "Red-cell distribution width in percent.",
    "alkaline_phosphatase_u_l": "Alkaline phosphatase in U/L.",
    "white_blood_cell_count_10e3_per_ul": "White-blood-cell count in thousands per microliter.",
    "cpg_sites": "All 20,318 unique canonical AltumAge CpG labels, in the same order as every beta_values row.",
    "beta_values": "20,318 methylation beta values in [0,1]. Nulls require missing_values=reference_median.",
    "missing_values": "Reject missing values or explicitly impute the pinned reference medians; no silent imputation.",
    "input_manifest": "Upload/finalize the scientific manifest first, then provide the returned artifact metadata.",
    "artifact_id": "Exact caller-owned uploaded artifact identifier; an example ID is not an existing upload.",
    "sha256": "Lowercase SHA-256 of the exact uploaded bytes.",
    "size_bytes": "Exact uploaded artifact size in bytes.",
    "media_type": "MIME type of the uploaded artifact.",
    "compression": "Compression applied to the uploaded artifact bytes.",
    "parameters": "Model-specific scientific parameters; the execution adapter performs remaining cross-field checks.",
    "service_class": "Queue service class supported by this profile; does not change the model's input format.",
    "operation": "Exact operation supported by this model profile.",
    "schema": "Canonical versioned request-contract identifier.",
    "client_context": "Optional caller-chosen correlation metadata, not inference parameters.",
    "seed": "Random seed for the selected runtime or scientific adapter.",
    "protocol": "BoltzGen design protocol matching the binder and target modalities in the input manifest.",
    "shard_id": "Unique identifier for this independently executed design shard.",
    "num_designs": "Candidate designs generated by this shard or run.",
    "budget": "Final designs retained from this BoltzGen shard; cannot exceed num_designs.",
    "reuse_completed": "Reuse completed stage outputs supplied to the BoltzGen workflow instead of regenerating them.",
    "variant": "Proteina-Complexa model variant matching the protein, ligand or AME target input.",
    "target_id": "Target identifier matching the target data in the uploaded manifest.",
    "run_name": "Caller-chosen name used to identify this design run's outputs.",
    "num_samples": "Number of candidate structures sampled by the model.",
    "diffusion_steps": "Number of diffusion denoising steps.",
    "target": "Target-chain and binding-hotspot selection for the uploaded target structure.",
    "chain": "Chain identifier in the uploaded target structure.",
    "hotspot_residues": "Target residues to contact; use this profile's integer or chain-residue notation.",
    "binder_length": "Binder length in amino-acid residues, or an inclusive minimum/maximum interval.",
    "minimum": "Inclusive minimum binder length in amino-acid residues.",
    "maximum": "Inclusive maximum binder length in amino-acid residues.",
    "designs": "Number of binder designs requested from the bounded BindCraft workflow.",
    "mpnn_lane": "ProteinMPNN weight family used for sequence design: vanilla or soluble.",
    "shard_count": "Number of independently executed Mosaic design shards.",
    "base_seed": "Base random seed from which shard-specific seeds are derived.",
    "hotspots": "Sorted unique one-based target-residue positions to contact.",
    "optimizer_steps": "Number of Mosaic binder-optimization iterations.",
    "contigs": "RFdiffusion expressions describing generated lengths and any fixed motif residues.",
    "diffuser_T": "RFdiffusion denoising timesteps.",
    "length": "Expected generated backbone length in amino-acid residues.",
    "input_pdb_artifact_id": "Uploaded motif PDB artifact ID; required only for motif scaffolding.",
    "motif_ca_rmsd_limit": "Maximum motif C-alpha RMSD in angstroms for semantic validation.",
    "mode": "Sequence input mode; only the enumerated modes are accepted by this profile.",
    "model_seeds": "Unique nonnegative random seeds for independent structure predictions.",
    "msa_mode": "Alignment mode; none performs no online MSA search or precomputed-MSA processing in this lane.",
    "checkpoint": "Exact pinned checkpoint family used by this scientific profile.",
    "sample_count": "Number of structures sampled per model seed.",
    "batch_id": "Caller-chosen batch correlation identifier, not a server-issued operation ID.",
    "correlation_id": "Caller-chosen identifier linking this run to an external workflow.",
    "display_name": "Human-readable label shown with this scientific run.",
}

_PURPOSES = {
    "qwen3-6-27b-fp8": "Caption and verify original videos/images with the pinned NVIDIA PAIDF reference VLM.",
    "qwen2-5-14b-instruct": (
        "Generate transformation prompts and verification questions with the pinned NVIDIA PAIDF reference LLM."
    ),
    "boltz2": "Predict proteins from chains and supplied A3M; returns mmCIF structures and confidence scores.",
    "openfold2": "Predict a protein from its sequence; returns ranked structure and confidence values.",
    "openfold3": "Predict a protein assembly from chains and optional supplied A3M; returns CIF structure results.",
    "diffdock": "Dock a SMILES ligand into a receptor PDB; returns ranked poses and docking confidence.",
    "proteinmpnn": "Design sequences compatible with a protein backbone PDB; returns sampled sequences and scores.",
    "genmol": (
        "Generate de-novo molecules using a SAFE mask-token minimum, not heavy-atom bounds; "
        "returns generated SMILES and property scores."
    ),
    "molmim": (
        "Run bounded CMA-ES/QED search from a SMILES molecule; returns distinct changed molecules or explicit "
        "GENERATION_EXHAUSTED when the requested count is not found. Property improvement is not guaranteed."
    ),
    "msa-search-pdb70": "Search the pinned local PDB70 database for a protein sequence; returns A3M alignments.",
    "sdxl": "Generate a 512x512 image from text; returns PNG bytes or the selected JSON/base64 envelope.",
    "nv-segment-ct": "Segment a CT NIfTI volume from labels or points; returns encoded segmentation and label counts.",
    "cellpose-cpsam-v2": (
        "Segment a bounded 2D microscopy image with Cellpose CPSAM v2; returns a labeled PNG mask, "
        "object count and per-object pixel areas."
    ),
    "scvi-scanvi": (
        "Fit scVI or scANVI to a bounded raw-count AnnData file; returns an integrated AnnData object, "
        "latent embeddings, run manifest and saved model as a ZIP artifact."
    ),
    "wan2-2-t2v-nim": (
        "Generate a bounded landscape or portrait MP4 from text with the pinned NVIDIA Wan2.2 NIM t2v deployment."
    ),
    "wan2-2-i2v-nim": (
        "Animate a caller-owned PNG or JPEG into a bounded MP4 with the pinned NVIDIA Wan2.2 NIM i2v deployment."
    ),
    "ace-step-1-5": (
        "Generate a bounded WAV music track from a text description and optional lyrics with pinned ACE-Step 1.5."
    ),
    "sam2-1-hiera-large": (
        "Segment a bounded image automatically or from point/box prompts, or track prompted objects through MP4 video; "
        "returns masks, metadata and a colorful overlay in a ZIP artifact."
    ),
    "cosmos3-nano": (
        "Generate or transform image/video from text and bounded media controls; large MP4 results are artifacts."
    ),
    "cosmos-transfer2-5-2b": (
        "Transform a caller-owned MP4 using text and full-video edge control with NVIDIA Cosmos Transfer 2.5; "
        "returns a silent MP4 artifact preserving source geometry, frame count and FPS. Built on NVIDIA Cosmos."
    ),
    "evo2-40b": "Continue a DNA sequence with Evo2-40B; returns generated DNA and elapsed milliseconds per token.",
    "altumage": "Predict methylation-based chronological age from the full CpG panel; returns age by sample ID.",
    "phenoage": "Calculate clinical phenotypic age from age and nine blood biomarkers; returns results by sample ID.",
    "qwen3-8b": "General-purpose text chat and reasoning; returns an OpenAI-compatible assistant completion and usage.",
    "nv-reason-cxr-3b": "Chest-X-ray reasoning from image and text; returns an assistant response, not a diagnosis.",
    "glm-5-2-fp8": "GLM text chat and reasoning; returns a completion when a compatible deployment is available.",
    "boltzgen": "Design protein, peptide, nanobody or antibody binders against manifest-supplied targets.",
    "proteina-complexa": "Generate binder candidates for a protein or ligand with the selected Complexa variant.",
    "bindcraft": "Design protein binders for a target chain and hotspots, including sequence design and filtering.",
    "mosaic": "Optimize protein binders against hotspots with the pinned Mosaic/Boltz2/ProteinMPNN workflow.",
    "rfdiffusion": "Generate protein backbones or scaffold a supplied structural motif using RFdiffusion contigs.",
    "esmfold2": "Predict protein structure with ESMFold2 from sequence and the optional manifest-carried MSA.",
    "esmfold2-fast": "Predict protein structure with the ESMFold2 fast single-sequence trunk; no MSA is supported.",
    "openfold3-openbind": "Predict structures with the independent OpenFold3/OpenBind batch backend, not AlphaFold3.",
    "protenix-v2": "Predict structures with pinned Protenix v2, selected seeds and the no-MSA input lane.",
    "alphafold3": "Predict complexes with AlphaFold3; raw input first runs the reference-data CPU pipeline.",
    "lammps": (
        "Run ordered native LAMMPS scripts for molecular/materials dynamics, minimization and analysis; "
        "returns trajectories, restart files, logs and hash-verified customer-bucket manifests."
    ),
    "namd": (
        "Run NVIDIA-packaged NAMD preparation and molecular-dynamics workflows from complete native inputs; "
        "returns trajectory segments, restart/bias state, logs and hash-verified customer-bucket manifests."
    ),
}


def _describe(schema: Any) -> None:
    if isinstance(schema, dict):
        for name, field in schema.get("properties", {}).items():
            if name in _DESCRIPTIONS:
                field.setdefault("description", _DESCRIPTIONS[name])
        for child in schema.values():
            _describe(child)
    elif isinstance(schema, list):
        for child in schema:
            _describe(child)


def _pydantic_contract(model_ref: str) -> tuple[Schema, tuple[str, ...]]:
    record = _resource("runtime-pydantic.json")[model_ref]
    schema = copy.deepcopy(record["schema"])
    props = schema["properties"]
    if model_ref == "boltz2":
        schema["description"] = (
            "Portable protein-only Boltz2, not the full NVIDIA NIM contract. Supply A3M explicitly. "
            "No ligands, affinity prediction, templates, seed or online MSA search."
        )
        schema["$defs"]["Polymer"]["properties"]["sequence"]["pattern"] = rf"^[{_PROTEIN.lower()}{_PROTEIN}\s]+$"
        a3m = schema["$defs"]["A3M"]["properties"]
        a3m["alignment"] = _transportable(
            a3m["alignment"],
            materialization="utf-8",
            media_types=("text/x-a3m", "text/plain"),
            max_bytes=16 * 1024 * 1024,
        )
    elif model_ref == "msa-search-pdb70":
        props["databases"]["const"] = ["pdb70_220313"]
        props["output_alignment_formats"]["const"] = ["a3m"]
        props["sequence"]["pattern"] = rf"^[{_PROTEIN.lower()}{_PROTEIN}\s]+$"
        schema["description"] = "Local MMseqs2 search against the pinned PDB70_220313 database."
    elif model_ref == "genmol":
        props["smiles"]["pattern"] = r"^\[\*\{[0-9]+-[0-9]+\}\]$"
        props["smiles"]["description"] = _GENMOL_MASK_DESCRIPTION
        props["scoring"]["pattern"] = r"^(?:[Qq][Ee][Dd]|[Ll][Oo][Gg][Pp])$"
        props["temperature"]["anyOf"][0]["exclusiveMinimum"] = 0
        props["noise"]["anyOf"][0]["minimum"] = 0
        schema["description"] = (
            _GENMOL_MASK_DESCRIPTION + " Range order and finite numeric strings are checked by the runtime."
        )
    elif model_ref == "molmim":
        schema["description"] = (
            "The search uses exactly particles * iterations model decodes and never silently expands this budget. "
            "num_molecules must not exceed that product; the runtime enforces this cross-field constraint. "
            "Finite-search exhaustion is not proof of chemical infeasibility. QED is a descriptor, not efficacy."
        )
        props["algorithm"]["description"] = "Actual adaptive CMA-ES with the requested fixed population and iterations."
        props["min_similarity"]["description"] = (
            "Hard final-output Morgan/Tanimoto similarity cutoff. It is also used in the soft CMA-ES objective; "
            "the hard output filter is stricter than upstream's soft-only score."
        )
        props["particles"]["description"] = "CMA-ES population size, at least two; part of the exact decode budget."
        props["iterations"]["description"] = "Requested CMA-ES ask/tell updates; no automatic increase on exhaustion."
        props["radius"]["description"] = (
            "Multiplier of the initial CMA-ES sigma 0.75; not a guaranteed chemical-distance radius."
        )
        props["minimize"]["description"] = (
            "Guide search toward lower QED and sort ascending; "
            "improvement over the starting molecule is not guaranteed."
        )
    elif model_ref == "altumage":
        props["cpg_sites"]["uniqueItems"] = True
        props["cpg_sites"] = _transportable(
            props["cpg_sites"],
            materialization="json",
            media_types=("application/json",),
            max_bytes=16 * 1024 * 1024,
        )
        schema["description"] = (
            "Methylation-based chronological-age prediction, not clinical PhenoAge. Requires the entire "
            "canonical 20,318-CpG panel. Generate a full synthetic example with "
            "models/aging/fixtures.py methylation_payload and the pinned model preprocessing assets; "
            "a shortened CpG example is not a valid request."
        )
        sample = schema["$defs"]["MethylationSample"]["properties"]
        sample["beta_values"]["items"]["examples"] = [0.5, None]
        sample["beta_values"] = _transportable(
            sample["beta_values"],
            materialization="json",
            media_types=("application/json",),
            max_bytes=16 * 1024 * 1024,
        )
    elif model_ref == "phenoage":
        schema["description"] = "Clinical PhenoAge from age plus nine blood biomarkers, not DNA-methylation PhenoAge."
    _describe(schema)
    return schema, (record["source"],)


def _openfold2() -> Schema:
    return _object(
        {
            "input_id": _field(
                "string", "Caller-chosen input identifier.", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
            ),
            "sequence": _field(
                "string",
                "Single canonical uppercase protein sequence.",
                minLength=1,
                maxLength=1024,
                pattern=rf"^[{_PROTEIN}]+$",
            ),
            "selected_models": _constant([1], "Exactly parameter set 1: model_3_ptm / finetuning_no_templ_ptm_1."),
            "relax_prediction": _constant(False, "Relaxation is not supported by this runtime."),
        },
        ("input_id", "sequence", "selected_models", "relax_prediction"),
        "Portable OpenFold2 with a single-sequence dummy MSA. No external MSA, template or relaxation fields.",
    )


def _openfold3() -> Schema:
    label = _field("string", "Unique input or chain label.", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    a3m = _object(
        {
            "alignment": _transportable(
                _field("string", "Literal nonempty A3M text.", minLength=1),
                materialization="utf-8",
                media_types=("text/x-a3m", "text/plain"),
                max_bytes=16 * 1024 * 1024,
            ),
            "format": _constant("a3m", "A3M alignment format."),
        },
        ("alignment", "format"),
        "Supplied A3M.",
    )
    molecule = _object(
        {
            "type": _constant("protein", "Only protein molecules are supported."),
            "id": label,
            "sequence": _field(
                "string", "Canonical uppercase amino-acid sequence.", minLength=1, pattern=rf"^[{_PROTEIN}]+$"
            ),
            "diffusion_samples": _constant(1, "The fixed Preview2 runtime generates exactly one sample."),
            "msa": _object(
                {"main": _object({"a3m": a3m}, ("a3m",), "Main alignment.")}, ("main",), "Optional supplied MSA."
            ),
        },
        ("type", "id", "sequence"),
        "Protein chain; chain IDs must be unique.",
    )
    entry = _object(
        {
            "input_id": label,
            "output_format": _constant("cif", "CIF output only."),
            "molecules": _array(molecule, "Protein chains with unique IDs.", minItems=1),
        },
        ("input_id", "output_format", "molecules"),
        "One folding input.",
    )
    return _object(
        {"request_id": label, "inputs": _array(entry, "Exactly one input.", minItems=1, maxItems=1)},
        ("request_id", "inputs"),
        "OpenFold3 Preview2, fixed seed 42 and one sample; no template or online MSA search.",
    )


def _diffdock() -> Schema:
    schema = _object(
        {
            "protein": _transportable(
                _field(
                    "string",
                    "Inline PDB containing ATOM records; 40–2,000,000 UTF-8 bytes (runtime checked).",
                    pattern="ATOM",
                ),
                materialization="utf-8",
                media_types=("chemical/x-pdb", "text/plain"),
                max_bytes=2_000_000,
                fixture_ids=("pdb/1ubq",),
            ),
            "ligand": _field("string", "Ligand SMILES, not an uploaded ligand path.", minLength=1, maxLength=4096),
            "ligand_file_type": _constant("txt", "Inline SMILES only."),
            "num_poses": _integer("Number of docking poses.", 1, 4, 1),
            "time_divisions": _integer("Diffusion time divisions; must be >= steps.", 3, 20, 20),
            "steps": _integer("Sampling steps, no greater than time_divisions.", 1, 20, 18),
            "random_seed": _integer("Random seed.", 1, 2**31 - 1, 1),
            "save_trajectory": {
                "enum": [False, None],
                "default": False,
                "description": "Trajectory output is unsupported.",
            },
            "skip_gen_conformer": {
                "enum": [False, None],
                "default": False,
                "description": "Skipping conformer generation is unsupported.",
            },
        },
        ("protein", "ligand"),
        "DiffDock v1.1 portable adapter; receptor PDB plus inline ligand SMILES.",
    )
    return schema


def _proteinmpnn() -> Schema:
    temperature = _field("number", "Finite sampling temperature.", minimum=0.01, maximum=1)
    return _object(
        {
            "input_pdb": _transportable(
                _field(
                    "string",
                    "Inline PDB with ATOM records; 40–2,000,000 UTF-8 bytes (runtime checked).",
                    pattern="ATOM",
                ),
                materialization="utf-8",
                media_types=("chemical/x-pdb", "text/plain"),
                max_bytes=2_000_000,
                fixture_ids=("pdb/1ubq",),
            ),
            "input_pdb_chains": {
                "anyOf": [
                    _array(
                        _field("string", "One-character chain ID.", minLength=1, maxLength=1),
                        "Chains to redesign.",
                        minItems=1,
                        maxItems=16,
                    ),
                    {"type": "null"},
                ],
                "description": "Existing chains to redesign; omitted/null means all chains.",
            },
            "random_seed": _integer("Random seed.", 1, 2**31 - 1, 1),
            "num_seq_per_target": _integer("Sequences sampled for the target.", 1, 8, 1),
            "sampling_temp": {
                "anyOf": [temperature, _array(temperature, "One temperature.", minItems=1, maxItems=1)],
                "default": [0.1],
                "description": "One finite temperature, supplied as a number or a one-item array.",
            },
            "omit_AAs": _array(
                {"type": "string", "enum": list(_PROTEIN + "X")}, "Residue codes excluded during design.", default=["X"]
            ),
        },
        ("input_pdb",),
        "ProteinMPNN v_48_020 portable adapter. Chain existence and PDB structure are checked by the runtime.",
    )


def _sdxl() -> Schema:
    return _object(
        {
            "prompt": _field("string", "Non-blank text describing the image.", minLength=1, pattern=r"\S"),
            "negative_prompt": {"type": ["string", "null"], "description": "Optional unwanted image content."},
            "seed": _field("integer", "Torch generator seed.", default=0),
            "steps": _integer("Denoising steps.", 1, 50, 20),
            "guidance": _field(
                "number",
                "Guidance strength; takes precedence over guidance_scale if both are supplied.",
                minimum=0,
                maximum=20,
                default=5,
            ),
            "guidance_scale": _field("number", "Alias used only when guidance is absent.", minimum=0, maximum=20),
            "width": _constant(512, "This qualified endpoint only supports 512-pixel width."),
            "height": _constant(512, "This qualified endpoint only supports 512-pixel height."),
            "response_format": _field(
                "string",
                "PNG bytes or a JSON/base64 envelope; use b64_json for JSON clients.",
                enum=["image/png", "b64_json"],
                default="image/png",
            ),
        },
        ("prompt",),
        "SDXL 512×512 PNG generation. Only fields consumed by the hosted adapter are advertised.",
    )


def _segment() -> Schema:
    schema = _object(
        {
            "input_nifti_base64": _transportable(
                _field(
                    "string",
                    "Base64-encoded NIfTI bytes (<=32 MiB decoded), finite 3D volume, each dimension 8–512.",
                    minLength=1,
                    contentEncoding="base64",
                ),
                materialization="base64",
                media_types=("application/x-nifti", "application/gzip", "application/octet-stream"),
                max_bytes=32 * 1024 * 1024,
                fixture_ids=("nifti/nv-segment-ct-synthetic-ellipsoid-v1",),
            ),
            "label_prompt": _array(
                _field("integer", "VISTA3D anatomical label index."), "Nonempty anatomical label prompts.", minItems=1
            ),
            "points": _array(
                _array(_field("number", "Voxel coordinate."), "One [x,y,z] voxel coordinate.", minItems=3, maxItems=3),
                "Point prompts in voxel coordinates.",
                minItems=1,
            ),
            "point_labels": _array(
                _field("integer", "VISTA3D point label (-1,0,1 or special flags 2,3).", enum=[-1, 0, 1, 2, 3]),
                "One label per point, in the same order.",
                minItems=1,
            ),
        },
        ("input_nifti_base64",),
        "NV-Segment-CT VISTA3D segmentation; provide label_prompt or points. Not a diagnosis.",
    )
    schema["anyOf"] = [{"required": ["label_prompt"]}, {"required": ["points", "point_labels"]}]
    schema["dependentRequired"] = {"points": ["point_labels"]}
    schema["allOf"] = [
        {"if": {"required": ["points", "label_prompt"]}, "then": {"properties": {"label_prompt": {"maxItems": 1}}}}
    ]
    return schema


def _cellpose() -> Schema:
    return _object(
        {
            "image_base64": _transportable(
                _field(
                    "string",
                    "Base64-encoded 2D microscopy image, at most 4 megapixels.",
                    minLength=4,
                    contentEncoding="base64",
                ),
                materialization="base64",
                media_types=("image/png", "image/jpeg", "image/tiff"),
                max_bytes=16 * 1024 * 1024,
            ),
            "media_type": _field(
                "string",
                "Exact media type of the image bytes.",
                enum=["image/png", "image/jpeg", "image/tiff"],
                default="image/png",
            ),
            "diameter": {
                "type": ["number", "null"],
                "exclusiveMinimum": 0,
                "maximum": 2048,
                "default": None,
                "description": "Optional expected object diameter in pixels; null lets CPSAM infer scale.",
            },
            "research_only": _constant(
                True,
                "Required acknowledgement for the research-only Cellpose checkpoint and its training-data terms.",
            ),
        },
        ("image_base64", "media_type", "research_only"),
        "Bounded 2D instance segmentation. The runtime rejects images above 4 megapixels.",
    )


def _scvi_scanvi() -> Schema:
    optional_key = {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 128,
    }
    return _object(
        {
            "anndata_base64": _transportable(
                _field(
                    "string",
                    "Base64-encoded raw-count AnnData .h5ad file for the bounded interactive lane.",
                    minLength=4,
                    contentEncoding="base64",
                ),
                materialization="base64",
                media_types=("application/x-hdf5", "application/octet-stream"),
                max_bytes=64 * 1024 * 1024,
            ),
            "filename": _field(
                "string",
                "Display filename ending in .h5ad; no path components.",
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.h5ad$",
                default="input.h5ad",
            ),
            "method": _field(
                "string",
                "scvi performs unsupervised integration; scanvi performs semi-supervised annotation after scVI.",
                enum=["scvi", "scanvi"],
                default="scvi",
            ),
            "batch_key": {
                **optional_key,
                "description": "Optional AnnData obs column containing batch labels.",
            },
            "labels_key": {
                **optional_key,
                "description": "AnnData obs label column; required for scanvi.",
            },
            "unlabeled_category": _field(
                "string",
                "Value in labels_key used for unlabeled cells during scanvi.",
                minLength=1,
                maxLength=128,
                default="Unknown",
            ),
            "max_epochs": _integer(
                "Training epochs in this interactive lane; scanvi applies the bound to both training stages.",
                1,
                20,
                20,
            ),
            "n_latent": _integer("Latent embedding dimensions.", 2, 64, 10),
            "seed": _field(
                "integer",
                "Nonnegative reproducibility seed.",
                minimum=0,
                maximum=2_147_483_647,
                default=0,
            ),
            "research_only": _constant(True, "Required acknowledgement that this is a research integration workflow."),
        },
        ("anndata_base64", "filename", "method", "max_epochs", "n_latent", "seed", "research_only"),
        "Bounded interactive scVI/scANVI lane. Use folder fan-out for multiple small files; larger studies need a "
        "scientific-batch profile rather than this synchronous adapter.",
    )


def _cosmos_transfer25() -> Schema:
    video = _artifact_reference(media_types=("video/mp4",))
    video["properties"]["size_bytes"].update(minimum=16, maximum=128 * 1024**2)
    video.update({
        "description": "Finalized caller-owned MP4 with 93–480 frames and arbitrary source resolution. "
        "This platform artifact transport is limited to 128 MiB, not a NVIDIA model byte limit. "
        "No workbench resize, crop, trim or retiming. "
        "Upload the actual file; URLs, local paths and inline base64 are not accepted by this public contract.",
        "x-fs2-artifact-materialization": "base64",
        "x-fs2-artifact-max-bytes": 128 * 1024**2,
        "x-fs2-artifact-media-types": ["video/mp4"],
    })
    return _object(
        {
            "video": video,
            "prompt": _field("string", "Describe the desired scene/weather while preserving source motion.",
                             minLength=1),
            "negative_prompt": _field("string", "Optional unwanted visual properties."),
            "seed": _integer("Deterministic sampling seed.", 0, 4294967295, 42),
            "num_steps": _field("integer", "Denoising steps; NVIDIA PAIDF reference uses 35.", minimum=1, default=35),
            "guidance": _integer("Integer prompt guidance; NVIDIA PAIDF reference uses 7.", 0, 7, 7),
            "resolution": _field("string", "Internal NIM processing resolution, not a source resize. PAIDF uses 720.",
                                 enum=["256", "480", "512", "720"], default="720"),
            "sigma_max": _field("number", "Maximum diffusion noise; NVIDIA PAIDF reference uses 90.", default=90),
            "edge": _object({"control_weight": _field("number", "Native edge-control strength.", minimum=0,
                                                      maximum=1, default=1)}, (), "NVIDIA reference edge control."),
            "control_weight": _field("number", "Legacy alias for edge.control_weight. Do not combine with edge.",
                                     minimum=0, maximum=1),
            "output_delivery": _constant("artifact", "Return the generated MP4 as a platform-owned artifact."),
        },
        ("video", "prompt"),
        "Bounded Cosmos Transfer 2.5 edge-conditioned video-to-video. Built on NVIDIA Cosmos. "
        "NIM guardrails remain enabled. Audio is not carried into the native result. "
        "This native App does not itself run PAIDF motion/weather verification, human approval or batch fan-out; "
        "generated media is not physical ground truth or validated annotation.",
    )


def _wan2_common() -> Schema:
    return {
        "prompt": _field("string", "Concrete visual scene and motion description.", minLength=1, maxLength=4096),
        "size": _field("string", "Output orientation and dimensions.", enum=["832x480", "480x832"], default="832x480"),
        "seconds": _integer(
            "Requested video duration in seconds; the final 16-fps clip may be slightly shorter.", 1, 12, 4
        ),
        "seed": _field(
            "integer",
            "Sampling seed; zero asks the NIM to choose a random seed.",
            minimum=0,
            maximum=4294967295,
            default=0,
        ),
        "steps": _integer("Diffusion steps.", 1, 100, 50),
        "cfg_scale": _field(
            "number", "Prompt guidance strength, strictly greater than one.", exclusiveMinimum=1, maximum=20, default=5
        ),
    }


def _wan2_t2v() -> Schema:
    return _object(
        _wan2_common(),
        ("prompt",),
        "Wan2.2 NIM text-to-video. Built-in NIM content filtering remains enabled. "
        "The result is a verified MP4 artifact.",
    )


def _wan2_i2v() -> Schema:
    properties = _wan2_common()
    properties["input_reference"] = _transportable(
        _field("string", "PNG or JPEG data URL materialized only at the runtime boundary.", minLength=1),
        materialization="data-url",
        media_types=("image/png", "image/jpeg"),
        max_bytes=16 * 1024 * 1024,
    )
    return _object(
        properties,
        ("prompt", "input_reference"),
        "Wan2.2 NIM image-to-video. Built-in NIM content filtering remains enabled. "
        "The result is a verified MP4 artifact.",
    )


def _ace_step() -> Schema:
    return _object(
        {
            "prompt": _field(
                "string",
                "Describe the genre, mood, instrumentation, pacing and production style.",
                minLength=1,
                maxLength=4096,
            ),
            "lyrics": _field(
                "string",
                "Optional lyrics. Omit or use [Instrumental] for music without vocals.",
                maxLength=12000,
                default="[Instrumental]",
            ),
            "duration_seconds": _field(
                "number", "Requested audio duration in seconds.", minimum=10, maximum=60, default=20
            ),
            "thinking": _field(
                "boolean", "Use the pinned 4B music language model for planning and audio codes.", default=True
            ),
            "seed": _field(
                "integer", "Deterministic generation seed.", minimum=0, maximum=4294967295, default=0
            ),
            "bpm": {
                "type": ["integer", "null"],
                "description": "Optional tempo in beats per minute; null lets the model choose.",
                "minimum": 30,
                "maximum": 300,
                "default": None,
            },
            "key_scale": _field(
                "string", "Optional musical key and scale, for example C major.", maxLength=32, default=""
            ),
            "time_signature": _field(
                "string",
                "Optional time signature.",
                enum=["", "2", "3", "4", "6", "2/4", "3/4", "4/4", "6/8"],
                default="",
            ),
            "vocal_language": _field(
                "string", "Language code used when lyrics contain vocals.", pattern=r"^[A-Za-z-]{2,16}$", default="en"
            ),
        },
        ("prompt",),
        "ACE-Step 1.5 text-to-music generation with one result per request. The qualified path returns a complete "
        "WAV, uses the turbo diffusion model and can use the pinned 4B planning model. Generated audio must be "
        "reviewed before publication.",
    )


def _sam2() -> Schema:
    point = _object(
        {
            "x": _field("number", "Horizontal pixel coordinate.", minimum=0, maximum=16384),
            "y": _field("number", "Vertical pixel coordinate.", minimum=0, maximum=16384),
            "label": _field("integer", "One selects foreground and zero selects background.", enum=[0, 1], default=1),
            "object_id": _field(
                "integer", "Positive object label shared by points for one object.", minimum=1, maximum=65535, default=1
            ),
        },
        ("x", "y"),
        "One positive or negative point prompt in source-pixel coordinates.",
    )
    media = _transportable(
        _field(
            "string",
            "Base64 media bytes materialized only at the runtime boundary.",
            minLength=4,
            contentEncoding="base64",
        ),
        materialization="base64",
        media_types=("image/png", "image/jpeg", "video/mp4"),
        max_bytes=64 * 1024 * 1024,
    )
    schema = _object(
        {
            "mode": _field(
                "string", "Segmentation workflow.", enum=["prompted-image", "automatic-image", "prompted-video"]
            ),
            "media_base64": media,
            "media_type": _field("string", "Exact uploaded media type.", enum=["image/png", "image/jpeg", "video/mp4"]),
            "points": _array(point, "Point prompts; group multiple objects with object_id.", maxItems=64),
            "box": _array(
                _field("number", "Box coordinate in source pixels.", minimum=0, maximum=16384),
                "Optional [x0,y0,x1,y1] box prompt.",
                minItems=4,
                maxItems=4,
            ),
            "object_id": _field(
                "integer", "Object label assigned to the optional box.", minimum=1, maximum=65535, default=1
            ),
            "prompt_frame": _field(
                "integer", "Zero-based video frame receiving the prompts.", minimum=0, maximum=319, default=0
            ),
            "max_masks": _integer("Maximum masks returned by automatic-image.", 1, 128, 32),
        },
        ("mode", "media_base64", "media_type"),
        "SAM 2.1 Hiera Large segmentation. Images are at most 2,073,600 pixels; videos are at most 320 frames. "
        "The output ZIP contains manifest.json, masks and an overlay image or MP4.",
    )
    schema["allOf"] = [
        {
            "if": {"properties": {"mode": {"const": "automatic-image"}}, "required": ["mode"]},
            "then": {
                "properties": {"media_type": {"enum": ["image/png", "image/jpeg"]}, "points": {"maxItems": 0}},
                "not": {"required": ["box"]},
            },
        },
        {
            "if": {"properties": {"mode": {"const": "prompted-image"}}, "required": ["mode"]},
            "then": {
                "properties": {"media_type": {"enum": ["image/png", "image/jpeg"]}},
                "anyOf": [
                    {"properties": {"points": {"minItems": 1}}, "required": ["points"]},
                    {"required": ["box"]},
                ],
            },
        },
        {
            "if": {"properties": {"mode": {"const": "prompted-video"}}, "required": ["mode"]},
            "then": {
                "properties": {"media_type": {"const": "video/mp4"}},
                "anyOf": [
                    {"properties": {"points": {"minItems": 1}}, "required": ["points"]},
                    {"required": ["box"]},
                ],
            },
        },
    ]
    return schema


def _cosmos() -> Schema:
    props = _cosmos_properties()
    schema = _object(
        props,
        ("prompt", "mode"),
        "Pinned Cosmos3 Nano media API. Media inputs use tenant artifacts or immutable HTTPS URLs; "
        "large MP4 outputs use the operation artifact store.",
    )
    # Keep the complete property map at the root for MCP discovery and artifact
    # materialization, but validate against exactly one mode-shaped request.
    # This prevents an incompatible field from reaching the GPU merely because
    # it is meaningful to some *other* Cosmos workflow.
    schema["oneOf"] = [_cosmos_generic_mode_schema(mode) for mode in _COSMOS_MODES]
    return schema


_COSMOS_MODES = (
    "text-to-image",
    "text-to-video",
    "image-to-video",
    "video-to-video",
    "transfer-video",
)
_COSMOS_SOURCE_REFS = (
    "https://huggingface.co/nvidia/Cosmos3-Nano/blob/7a312c868bcce8e40b3eb40861300a9d0ba3fde1/README.md",
    "https://github.com/vllm-project/vllm-omni/blob/eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c/recipes/cosmos3/Cosmos3-Nano.md",
)


def _cosmos_reference(description: str, *, media_types: tuple[str, ...], max_bytes: int = 512 * 1024 * 1024) -> Schema:
    return _transportable(
        _field(
            "string",
            description + " Use the final immutable HTTPS URL; redirects and customer-local paths are rejected.",
            pattern=r"^https://",
            maxLength=4096,
        ),
        materialization="data-url",
        media_types=media_types,
        max_bytes=max_bytes,
    )


def _cosmos_control() -> Schema:
    reference = _cosmos_reference(
        "Optional control image/video. Required for depth, segmentation and WSM; "
        "edge/blur may derive from input_reference.",
        media_types=("image/jpeg", "image/png", "image/webp", "video/mp4", "application/mp4"),
        max_bytes=128 * 1024 * 1024,
    )
    schema = _object(
        {
            "control_type": _field(
                "string", "Transfer representation supplied to Cosmos.", enum=["edge", "blur", "depth", "seg", "wsm"]
            ),
            "reference": reference,
            "control_weight": _field(
                "number",
                "Nonnegative relative control weight; at least one selected control must be positive.",
                minimum=0,
                maximum=100,
                default=1,
            ),
            "edge_threshold": _field(
                "string",
                "Edge extraction preset; valid only for edge control.",
                enum=["none", "very_low", "low", "medium", "high", "very_high"],
            ),
            "blur_strength": _field(
                "string",
                "Blur extraction preset; valid only for blur control.",
                enum=["none", "very_low", "low", "medium", "high", "very_high"],
            ),
        },
        ("control_type",),
        "One typed transfer control. Control types may not repeat in a request.",
    )
    schema["allOf"] = [
        {
            "if": {
                "properties": {"control_type": {"enum": ["depth", "seg", "wsm"]}},
                "required": ["control_type"],
            },
            "then": {"required": ["reference"]},
        },
        {
            "if": {
                "properties": {"control_type": {"not": {"const": "edge"}}},
                "required": ["control_type"],
            },
            "then": {"not": {"required": ["edge_threshold"]}},
        },
        {
            "if": {
                "properties": {"control_type": {"not": {"const": "blur"}}},
                "required": ["control_type"],
            },
            "then": {"not": {"required": ["blur_strength"]}},
        },
    ]
    return schema


def _cosmos_properties() -> Schema:
    input_reference = _cosmos_reference(
        "Reference image or MP4 selected by mode.",
        media_types=("image/jpeg", "image/png", "image/webp", "video/mp4", "application/mp4"),
    )
    return {
        "model": _constant(
            "nvidia/Cosmos3-Nano@7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
            "Exact pinned model identity; omit to use this fixed deployment.",
        ),
        "mode": _field("string", "Exact Cosmos workflow; prefer its dedicated MCP tool.", enum=list(_COSMOS_MODES)),
        "prompt": _field(
            "string",
            "Scene description or robotics instruction. Structured upsampled prompts are accepted as strings.",
            minLength=1,
            maxLength=4096,
        ),
        "negative_prompt": _field("string", "Content and artifacts to avoid.", maxLength=4096, default=""),
        "seed": _field("integer", "Deterministic sampling seed.", minimum=0, maximum=4294967295, default=0),
        "num_inference_steps": _integer("Denoising steps.", 1, 50, 30),
        "guidance_scale": _field("number", "Prompt guidance strength.", minimum=0, maximum=20, default=7),
        "size": _field(
            "string",
            "WIDTHxHEIGHT; each dimension is a multiple of 16, width 256–1280, height 256–720 and area <=921600.",
            pattern=r"^[0-9]+x[0-9]+$",
            minLength=7,
            maxLength=9,
        ),
        "num_frames": _integer("Generated video frames (5–400).", 5, 400, 25),
        "fps": _integer("Generated video frames per second.", 1, 30, 24),
        "input_reference": input_reference,
        "vision_path": _field(
            "string",
            "Deprecated compatibility alias for input_reference. Only an HTTPS URL is accepted; "
            "a customer-local path never is.",
            pattern=r"^https://",
            maxLength=4096,
        ),
        "generate_sound": _field(
            "boolean", "Generate synchronized AAC audio for text-to-video or image-to-video.", default=False
        ),
        "sound_duration": _field(
            "number", "Generated audio seconds; requires generate_sound=true.", exclusiveMinimum=0, maximum=30
        ),
        "condition_frame_indexes_vision": _array(
            _field("integer", "Nonnegative conditioned latent-frame index.", minimum=0, maximum=100),
            "Selected latent frames used for V2V continuation, not full-trajectory preservation. "
            "[0] reads one source pixel frame; [0,1] reads five. Unconditioned future motion is generated.",
            minItems=1,
            maxItems=16,
            uniqueItems=True,
        ),
        "condition_video_keep": _field(
            "string",
            "Decode the needed reference frames from the beginning or end.",
            enum=["first", "last"],
            default="first",
        ),
        "controls": _array(_cosmos_control(), "One or more typed transfer controls.", minItems=1, maxItems=5),
        "resolution": _field(
            "integer",
            "Legacy transfer resolution hint retained for compatibility. The hosted runtime uses explicit "
            "size for output dimensions (448x256 when omitted); this hint does not preserve source aspect ratio.",
            enum=[256, 480, 704, 720],
            default=480,
        ),
        "control_guidance": _field("number", "Transfer control guidance strength.", minimum=0, maximum=20, default=1.5),
        "control_guidance_interval": _array(
            _field("number", "Inclusive denoising fraction.", minimum=0, maximum=1),
            "Ordered [start,end] transfer-guidance interval.",
            minItems=2,
            maxItems=2,
        ),
        "num_video_frames_per_chunk": _integer("Transfer chunk size.", 5, 400, 93),
        "num_conditional_frames": _integer("Conditional frames per transfer chunk.", 1, 16, 1),
        "num_first_chunk_conditional_frames": _integer("Additional first-chunk conditional frames.", 0, 16, 0),
        "share_vision_temporal_positions": _field(
            "boolean", "Share control/target temporal positions in transfer mode.", default=True
        ),
        "emphasize_control_in_prompt": _field(
            "boolean", "Append the pinned control-adherence directive in transfer mode.", default=True
        ),
        "output_format": _field("string", "Mode-specific output format.", enum=["png", "mp4"]),
        "output_delivery": _field(
            "string",
            "Video delivery only: use the operation artifact store or legacy inline text-to-video. "
            "Omit for text-to-image, which returns the legacy PNG JSON envelope.",
            enum=["inline-base64", "artifact"],
        ),
    }


def _cosmos_mode_schema(mode: str) -> Schema:
    props = _cosmos_properties()
    allowed: dict[str, tuple[str, ...]] = {
        "text-to-image": (
            "model",
            "prompt",
            "negative_prompt",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "size",
        ),
        "text-to-video": (
            "model",
            "prompt",
            "negative_prompt",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "size",
            "num_frames",
            "fps",
            "generate_sound",
            "sound_duration",
        ),
        "image-to-video": (
            "model",
            "prompt",
            "negative_prompt",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "size",
            "num_frames",
            "fps",
            "input_reference",
            "vision_path",
            "generate_sound",
            "sound_duration",
        ),
        "video-to-video": (
            "model",
            "prompt",
            "negative_prompt",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "size",
            "num_frames",
            "fps",
            "input_reference",
            "vision_path",
            "condition_frame_indexes_vision",
            "condition_video_keep",
        ),
        "transfer-video": (
            "model",
            "prompt",
            "negative_prompt",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "size",
            "num_frames",
            "fps",
            "input_reference",
            "vision_path",
            "controls",
            "resolution",
            "control_guidance",
            "control_guidance_interval",
            "num_video_frames_per_chunk",
            "num_conditional_frames",
            "num_first_chunk_conditional_frames",
            "share_vision_temporal_positions",
            "emphasize_control_in_prompt",
        ),
    }
    required = ["prompt"]
    if mode in {"image-to-video", "video-to-video"}:
        required.append("input_reference")
    if mode == "transfer-video":
        required.append("controls")
    schema = _object(
        {name: props[name] for name in allowed[mode]},
        tuple(required),
        f"Typed {mode} inputs for the pinned Cosmos3 Nano runtime.",
    )
    if "vision_path" in schema["properties"]:
        if mode in {"image-to-video", "video-to-video"}:
            schema["oneOf"] = [{"required": ["input_reference"]}, {"required": ["vision_path"]}]
            schema["required"].remove("input_reference")
        else:
            schema.setdefault("allOf", []).append({"not": {"required": ["input_reference", "vision_path"]}})
    if "sound_duration" in schema["properties"]:
        schema.setdefault("allOf", []).append(
            {
                "if": {"required": ["sound_duration"]},
                "then": {"properties": {"generate_sound": {"const": True}}, "required": ["generate_sound"]},
            }
        )
    return schema


def _cosmos_generic_mode_schema(mode: str) -> Schema:
    """Add the discriminator and caller-visible delivery fields to one strict mode."""

    schema = _cosmos_mode_schema(mode)
    properties = schema["properties"]
    properties["mode"] = _constant(mode, f"Select the {mode} Cosmos workflow.")
    schema["required"].append("mode")
    if mode == "text-to-image":
        properties["output_format"] = _constant("png", "PNG output only.")
    elif mode == "text-to-video":
        properties["output_format"] = _constant("mp4", "MP4 output only.")
        properties["output_delivery"] = _field(
            "string",
            "Return legacy inline base64 or externalize the MP4 through the operation artifact store.",
            enum=["inline-base64", "artifact"],
        )
    else:
        properties["output_format"] = _constant("mp4", "MP4 output only.")
        properties["output_delivery"] = _constant("artifact", "Store MP4 bytes as a tenant-owned operation artifact.")
    disallowed = sorted(set(_cosmos_properties()) - set(properties))
    if disallowed:
        schema.setdefault("allOf", []).append({"not": {"anyOf": [{"required": [field]} for field in disallowed]}})
    # The outer schema owns the complete public property allow-list. Keeping
    # this branch open lets the MCP layer add its idempotency/wait controls
    # without weakening the mode-specific model-field exclusions above.
    schema["additionalProperties"] = True
    return schema


COSMOS_SPECIALIZED_TOOL_NAMES = frozenset(
    {
        "cosmos3_nano_text_to_image",
        "cosmos3_nano_text_to_video",
        "cosmos3_nano_image_to_video",
        "cosmos3_nano_video_to_video",
        "cosmos3_nano_transfer_video",
    }
)


def cosmos_specialized_contracts(
    model: OperationalModel,
) -> tuple[tuple[str, ModelInputContract, dict[str, Any], str, str], ...]:
    """Task-specific Cosmos media tools qualified on the exact pinned runtime."""

    if model.id != "cosmos3-nano" or "native" not in model.gateway.protocols:
        return ()
    model_ref = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    if model_ref != "cosmos3-nano":
        return ()
    specs = (
        (
            "cosmos3_nano_text_to_image",
            "text-to-image",
            "Cosmos text to image",
            "Generate one bounded PNG from text and return the legacy inline base64 envelope. "
            "GPU work can queue and take minutes; submit once and poll the returned operation ID.",
        ),
        (
            "cosmos3_nano_text_to_video",
            "text-to-video",
            "Cosmos text to video",
            "Generate a 5–400 frame MP4 from text, optionally with synchronized AAC audio. "
            "The asynchronous result is an artifact; GPU work can queue and take minutes.",
        ),
        (
            "cosmos3_nano_image_to_video",
            "image-to-video",
            "Cosmos image to video",
            "Animate a tenant artifact or immutable HTTPS image into a 5–400 frame MP4, optionally with "
            "synchronized AAC audio. Work can queue and take minutes; use text-to-video without image control.",
        ),
        (
            "cosmos3_nano_video_to_video",
            "video-to-video",
            "Cosmos video to video",
            "Continue an MP4 of at most 512 MiB from selected first/last latent frames. Future motion is "
            "generated, not preserved from the full source clip or recorded robot actions. The queued result "
            "is an MP4 artifact; use transfer-video for full-sequence spatial controls and validate motion separately.",
        ),
        (
            "cosmos3_nano_transfer_video",
            "transfer-video",
            "Cosmos controlled video transfer",
            "Generate a queued MP4 guided across the reference sequence by edge, blur, depth, segmentation "
            "or WSM controls. This does not guarantee object identity, robot contacts or action alignment. "
            "The result can take minutes; transfer cannot be combined with sound or robotics action.",
        ),
    )
    examples: dict[str, dict[str, Any]] = {
        "text-to-image": {"prompt": "A red cube on a white table", "size": "512x512", "seed": 1},
        "text-to-video": {
            "prompt": "A robot arm places a pear in a basket",
            "size": "448x256",
            "num_frames": 25,
            "fps": 24,
            "seed": 1,
        },
        "image-to-video": {
            "prompt": "The scene comes to life with smooth natural motion.",
            "input_reference": {
                "artifact_id": "00000000-0000-4000-8000-000000000011",
                "sha256": "1" * 64,
                "size_bytes": 1024,
                "media_type": "image/png",
                "compression": "none",
            },
        },
        "video-to-video": {
            "prompt": "Continue the same scene with consistent subjects and lighting.",
            "input_reference": {
                "artifact_id": "00000000-0000-4000-8000-000000000012",
                "sha256": "2" * 64,
                "size_bytes": 4096,
                "media_type": "video/mp4",
                "compression": "none",
            },
            "condition_frame_indexes_vision": [0, 1],
            "condition_video_keep": "first",
        },
        "transfer-video": {
            "prompt": "Preserve the scene while following the depth control.",
            "size": "640x480",
            "controls": [
                {
                    "control_type": "depth",
                    "reference": {
                        "artifact_id": "00000000-0000-4000-8000-000000000013",
                        "sha256": "3" * 64,
                        "size_bytes": 4096,
                        "media_type": "video/mp4",
                        "compression": "none",
                    },
                }
            ],
        },
    }
    return tuple(
        (
            name,
            ModelInputContract(_cosmos_mode_schema(mode), (examples[mode],), _COSMOS_SOURCE_REFS, model_ref, "native"),
            {
                "mode": mode,
                # The pinned TextToImageRequest returns legacy JSON and forbids
                # output_delivery; only the video request DTOs accept it.
                **({} if mode == "text-to-image" else {"output_delivery": "artifact"}),
                "output_format": "png" if mode == "text-to-image" else "mp4",
            },
            title,
            description,
        )
        for name, mode, title, description in specs
    )


def _evo2() -> Schema:
    properties = {
        "sequence": _field(
            "string",
            "DNA prompt; A,C,G,T,N in either case, normalized uppercase.",
            minLength=1,
            maxLength=8192,
            pattern=r"^[ACGTNacgtn]+$",
        ),
        "num_tokens": _field("integer", "Number of new DNA tokens to generate.", minimum=1, maximum=512),
        "temperature": _field("number", "Finite positive sampling temperature.", exclusiveMinimum=0),
        "top_k": _field("integer", "Number of highest-scoring token candidates retained.", minimum=1, maximum=512),
        "top_p": _field("number", "Nucleus-sampling probability; 0 disables this filter.", minimum=0, maximum=1),
        "random_seed": _field("integer", "Nonnegative 63-bit random seed.", minimum=0, maximum=2**63 - 1),
        "enable_logits": _constant(False, "Logit output is not exposed by the lean runtime."),
        "enable_sampled_probs": _constant(False, "Sampled-probability output is not exposed."),
        "enable_elapsed_ms_per_token": _constant(True, "Return the measured elapsed milliseconds per generated token."),
    }
    return _object(properties, tuple(properties), "Exact Evo2-40B lean runtime: all nine fields must be present.")


def _rfdiffusion() -> Schema:
    return _object(
        {
            "input_pdb": _transportable(
                _field(
                    "string",
                    "Inline input protein PDB, beginning with an ATOM or HEADER record.",
                    pattern=r"^(ATOM|HEADER)",
                ),
                materialization="utf-8",
                media_types=("chemical/x-pdb", "text/plain"),
                max_bytes=2_000_000,
                fixture_ids=("pdb/1ubq",),
            ),
            "contigs": _field(
                "string",
                "RFdiffusion contig expression describing fixed motifs and generated segments.",
                minLength=1,
                maxLength=256,
                pattern=r"^[A-Za-z0-9/ .,_-]{1,256}$",
            ),
            "diffusion_steps": _field("integer", "Number of denoising timesteps.", minimum=1, maximum=100),
            "random_seed": _field("integer", "Deterministic design seed.", minimum=0, maximum=2**31 - 1),
        },
        ("input_pdb", "contigs", "diffusion_steps", "random_seed"),
        "Archived portable native RFdiffusion contract; returns an output PDB. Current batch deployments instead "
        "use the separate scientific-batch-v1 schema. This description does not enable the native route.",
    )


_CHAT_DESCRIPTIONS = {
    "messages": "Conversation messages. Text is supported; only NV-Reason-CXR also accepts image_url content parts.",
    "frequency_penalty": "Penalize tokens according to their frequency in generated text.",
    "presence_penalty": "Penalize tokens that have already appeared in generated text.",
    "logit_bias": "Token-ID strings mapped to sampling logit biases.",
    "logprobs": "Request token log probabilities when supported by the runtime configuration.",
    "top_logprobs": "Number of alternative token log probabilities returned at each position.",
    "max_tokens": "Maximum generated tokens; deprecated by the runtime in favor of max_completion_tokens.",
    "max_completion_tokens": "Maximum generated completion tokens, including reasoning tokens when applicable.",
    "n": "Number of completion choices generated for this conversation.",
    "response_format": "Text, JSON-object or JSON-schema output constraint, subject to the model's output backend.",
    "thinking_token_budget": "Maximum reasoning tokens before thinking ends, for models supporting a thinking budget.",
    "seed": "Signed 64-bit generation seed; reproducibility also depends on the runtime and request.",
    "stop": "Stop string or strings that end generation.",
    "stream": "This operation-based gateway returns completed results, not a streaming response; use false or omit.",
    "stream_options": "Upstream streaming options; inactive for this gateway's non-streaming operation result.",
    "temperature": "Sampling temperature. Omit to use the selected runtime generation configuration.",
    "top_p": "Nucleus-sampling probability. Omit to use the runtime generation configuration.",
    "tools": "Function definitions made available to the chat model; tool arguments remain caller-defined JSON Schema.",
    "tool_choice": "Disable, allow, require or select a function call; depends on the deployed tool parser.",
    "parallel_tool_calls": "Whether the chat model may request multiple function calls in one response.",
    "user": "Legacy upstream user label; vLLM ignores it. Platform ownership always comes from the authenticated key.",
    "use_beam_search": "Use the runtime's beam-search decoding instead of ordinary sampling.",
    "top_k": "Top-k sampling candidate count; availability/default follows the runtime sampling implementation.",
    "min_p": "Minimum token probability relative to the most likely token.",
    "repetition_penalty": "Multiplicative penalty for repeated tokens.",
    "length_penalty": "Length penalty used by beam-search decoding.",
    "stop_token_ids": "Token IDs that terminate generation.",
    "include_stop_str_in_output": "Keep the matched stop string in generated text.",
    "ignore_eos": "Continue generation past the model end-of-sequence token, subject to other limits.",
    "min_tokens": "Minimum generated tokens before stop/end-of-sequence termination is permitted.",
    "skip_special_tokens": "Omit special tokenizer tokens from decoded text.",
    "spaces_between_special_tokens": "Insert spaces between decoded special tokens.",
    "truncate_prompt_tokens": "Prompt truncation token count; -1 uses the model's maximum context handling.",
    "prompt_logprobs": "Number of prompt-token log probabilities requested from the runtime.",
    "allowed_token_ids": "Restrict generated tokens to the supplied vocabulary token IDs.",
    "bad_words": "Text sequences excluded from generated output.",
    "include_reasoning": "Include parsed reasoning content in the assistant response when the model supports it.",
    "priority": "Upstream request priority; nonzero values require the deployed priority scheduler.",
    "role": "Conversation speaker or tool role interpreted by the model chat template.",
    "content": "Message text or content parts supported by this model's modalities.",
    "text": "Literal text content of this message part.",
    "image_url": "Image URL or image data URI for NV-Reason-CXR; not supported by text-only models.",
    "url": "Source URL or data URI for the image content.",
    "detail": "Image detail preference passed to the multimodal processor.",
    "name": "Function, speaker or tool name, according to the containing object.",
    "tool_call_id": "ID linking a tool response to the originating assistant tool call.",
    "tool_calls": "Function calls previously requested by an assistant message.",
    "function": "Function identity and arguments or its declared callable schema.",
    "arguments": "JSON-encoded arguments for a function call.",
    "parameters": "Caller-defined JSON Schema for a tool's arguments; not a model inference payload wrapper.",
    "refusal": "Refusal text from a prior assistant response.",
    "reasoning": "Reasoning text carried by a prior assistant message, if supported by the chat template.",
    "type": "Discriminator selecting the declared content, tool or output-format variant.",
}


def _chat(model_ref: str) -> tuple[Schema, tuple[str, ...]]:
    record = _resource("vllm-chat.json")
    definitions = copy.deepcopy(record["schemas"])
    schema = definitions.pop("ChatCompletionRequest")
    schema["properties"].pop("model", None)  # The named tool supplies its own public route.
    schema["properties"]["stream"] = _constant(False, _CHAT_DESCRIPTIONS["stream"])
    schema["additionalProperties"] = False
    # The generic vLLM OpenAPI advertises modalities for many other models.
    # Restrict only message content to this catalog model's supported modalities.
    allowed_parts = {
        "ChatCompletionContentPartTextParam",
        "ChatCompletionContentPartRefusalParam",
        "CustomChatCompletionContentToolReferenceParam",
        "CustomThinkCompletionContentParam",
    }
    if model_ref == "nv-reason-cxr-3b":
        allowed_parts |= {"ChatCompletionContentPartImageParam", "CustomChatCompletionContentSimpleImageParam"}
        image_url = definitions.get("ImageURL", {}).get("properties", {})
        if "url" in image_url:
            image_url["url"] = _transportable(
                image_url["url"],
                materialization="data-url",
                media_types=("image/png", "image/jpeg", "image/webp"),
                max_bytes=16 * 1024 * 1024,
            )
        simple_image = definitions.get("CustomChatCompletionContentSimpleImageParam", {}).get("properties", {})
        if "image_url" in simple_image:
            simple_image["image_url"] = _transportable(
                simple_image["image_url"],
                materialization="data-url",
                media_types=("image/png", "image/jpeg", "image/webp"),
                max_bytes=16 * 1024 * 1024,
            )
    for name in allowed_parts:
        definitions[name]["additionalProperties"] = False
    messages = schema["properties"]["messages"]["items"]["anyOf"]
    messages[:] = [part for part in messages if not part.get("$ref", "").endswith("openai_harmony__Message")]
    for message in messages:
        definition = definitions[message["$ref"].rsplit("/", 1)[1]]
        definition["additionalProperties"] = False
        definition["properties"].pop("audio", None)
        content = definition["properties"].get("content", {})
        for variant in content.get("anyOf", []):
            choices = variant.get("items", {}).get("anyOf")
            if choices is not None:
                choices[:] = [
                    choice
                    for choice in choices
                    if "$ref" not in choice or choice["$ref"].rsplit("/", 1)[1] in allowed_parts
                ]
    # Rebase OpenAPI refs and retain only reachable definitions. Unused audio,
    # video and Harmony schemas must not imply unsupported model capabilities.
    used: set[str] = set()

    def refs(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                value["$ref"] = f"#/$defs/{name}"
                if name not in used:
                    used.add(name)
                    refs(definitions[name])
            for child in value.values():
                refs(child)
        elif isinstance(value, list):
            for child in value:
                refs(child)

    refs(schema)
    schema["$defs"] = {name: definitions[name] for name in sorted(used)}

    def describe(value: Any) -> None:
        if isinstance(value, dict):
            for name, field in value.get("properties", {}).items():
                if name in _CHAT_DESCRIPTIONS:
                    if not field.get("description"):
                        field["description"] = _CHAT_DESCRIPTIONS[name]
            for child in value.values():
                describe(child)
        elif isinstance(value, list):
            for child in value:
                describe(child)

    describe(schema)
    schema["description"] = (
        "OpenAI chat request projected from the exact hosted vLLM 0.28.0 OpenAPI. The named tool binds model; "
        "do not supply it. Optional parser, template, caching, structured-output and scheduler features depend "
        "on the deployed model configuration, not merely the generic vLLM schema. Audio/video are unsupported."
    )
    return schema, (
        "oci://vllm-openai@" + record["source"]["image"] + "#/openapi.json/ChatCompletionRequest",
        "https://github.com/vllm-project/vllm/blob/"
        + record["source"]["revision"]
        + "/vllm/entrypoints/openai/chat_completion/protocol.py",
    )


_NATIVE_BUILDERS = {
    "rfdiffusion": (_rfdiffusion, "k8s-inference/models/structure/runtime/rfdiffusion/server.py"),
    "evo2-40b": (
        _evo2,
        "oci://evo2-40b@sha256:383f9979021bd3fe018c4dbba675610e0a5f7282b2164db1d8386611351de6f5"
        "#/opt/evo2/server/evo2_deep/runtime.py",
    ),
    "cosmos3-nano": (_cosmos, "k8s-inference/models/general-media/k8s/cosmos3-nano.yaml#data.adapter.py"),
    "cosmos-transfer2-5-2b": (
        _cosmos_transfer25,
        "k8s-inference/models/general-media/cosmos-transfer25/adapter/app.py",
    ),
    "openfold2": (_openfold2, "k8s-inference/models/structure/openfold2-upstream/server.py"),
    "openfold3": (_openfold3, "k8s-inference/models/structure/openfold3-preview2/server.py"),
    "diffdock": (_diffdock, "k8s-inference/models/structure/runtime/adapters/diffdock.py"),
    "proteinmpnn": (_proteinmpnn, "k8s-inference/models/structure/runtime/adapters/proteinmpnn.py"),
    "sdxl": (_sdxl, "k8s-inference/models/general-media/sdxl_server.py"),
    "nv-segment-ct": (_segment, "k8s-inference/models/general-media/nv_segment_ct_server.py"),
    "cellpose-cpsam-v2": (
        _cellpose,
        "k8s-inference/models/visual-science/cellpose-cpsam-v2/app.py",
    ),
    "scvi-scanvi": (
        _scvi_scanvi,
        "k8s-inference/models/visual-science/scvi-scanvi/app.py",
    ),
    "wan2-2-t2v-nim": (
        _wan2_t2v,
        "k8s-inference/models/general-media/wan2-adapter/app.py",
    ),
    "wan2-2-i2v-nim": (
        _wan2_i2v,
        "k8s-inference/models/general-media/wan2-adapter/app.py",
    ),
    "ace-step-1-5": (
        _ace_step,
        "k8s-inference/models/general-media/ace-step/adapter/app.py",
    ),
    "sam2-1-hiera-large": (
        _sam2,
        "k8s-inference/models/visual-science/sam2/app.py",
    ),
}


def _check_adapter(model: OperationalModel, model_ref: str) -> None:
    expected = _resource("runtime-adapters.json").get(model_ref)
    qualification = model.gateway.qualification or {}
    origin = qualification.get("runtime_origin") or {}
    variants = {
        value
        for value in (
            model.variant_id,
            qualification.get("variant_id"),
            origin.get("variant_id"),
        )
        if isinstance(value, str)
    }
    if expected is not None:
        if model.gateway.runtime_kind != expected["runtime_kind"]:
            raise InputContractUnavailable(
                f"{model_ref}: {model.gateway.runtime_kind} is not the reviewed {expected['variant_id']} adapter"
            )
        if any(variant != expected["variant_id"] for variant in variants):
            raise InputContractUnavailable(
                f"{model_ref}: selected runtime variant has no matching reviewed input contract"
            )
    elif model_ref == "rfdiffusion" and model.gateway.runtime_kind == "nim":
        raise InputContractUnavailable(
            "RFdiffusion's archived NIM interface is not its portable native or batch adapter"
        )


def _paidf_chat(model_ref: str) -> tuple[Schema, tuple[str, ...]]:
    schema = copy.deepcopy(_resource("paidf-chat.json")[model_ref])

    def media_fields(value: Any) -> None:
        if isinstance(value, dict):
            if "x-reference-media-types" in value:
                media_types = value.pop("x-reference-media-types")
                maximum = value.pop("x-reference-max-bytes")
                field = _transportable(copy.deepcopy(value), materialization="data-url",
                                       media_types=tuple(media_types), max_bytes=maximum)
                value.clear()
                value.update(field)
                return
            for child in value.values():
                media_fields(child)
        elif isinstance(value, list):
            for child in value:
                media_fields(child)

    media_fields(schema)
    source = schema["properties"]["model"]["const"]
    return schema, (
        "k8s-inference/models/general-media/paidf-chat/adapter/contracts.py",
        "https://huggingface.co/" + source + "/tree/" + schema["x-scientific-source-revision"],
        "https://github.com/NVIDIA/paidf-augmentation/tree/bc5719362492a1e3b40bd7d33b43c46dd89efad5",
    )


def contract_for(model: OperationalModel, protocol: str) -> ModelInputContract:
    """Resolve clones by source identity, without weakening route admission."""
    model_ref = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    _check_adapter(model, model_ref)
    if protocol not in model.gateway.protocols:
        raise InputContractUnavailable(f"{model_ref} does not publish protocol {protocol}")
    if protocol == "native" and model_ref in _resource("voice.json"):
        schema = copy.deepcopy(_resource("voice.json")[model_ref])
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        return ModelInputContract(
            schema, (), ("k8s-inference/components/voice-runtime/src/fs2_voice/contracts.py",), model_ref, protocol
        )
    if protocol == "native" and model_ref in _resource("speech.json"):
        schema = copy.deepcopy(_resource("speech.json")[model_ref])
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        return ModelInputContract(
            schema,
            (),
            (
                "k8s-inference/components/speech-runtime/src/fs2_speech/contracts.py",
                "k8s-inference/components/speech-runtime/src/fs2_speech/audio.py",
            ),
            model_ref,
            protocol,
        )
    if protocol == "native" and model_ref in _resource("paidf-chat.json"):
        schema, refs = _paidf_chat(model_ref)
    elif protocol == "native" and model_ref in _resource("runtime-pydantic.json"):
        schema, refs = _pydantic_contract(model_ref)
    elif protocol == "native" and model_ref in _NATIVE_BUILDERS:
        builder, source = _NATIVE_BUILDERS[model_ref]
        schema, refs = builder(), (source,)
        if model_ref == "nv-segment-ct":
            refs += (
                "https://huggingface.co/nvidia/NV-Segment-CT/blob/"
                "afb51518689f71e6abb367ee6301b2cd0225c66a/vista3d_pipeline.py",
            )
    elif protocol == "openai-chat" and model_ref in {"qwen3-8b", "nv-reason-cxr-3b", "glm-5-2-fp8"}:
        schema, refs = _chat(model_ref)
    else:
        raise InputContractUnavailable(f"No reviewed input contract for {model_ref}/{protocol}")
    schema["description"] = (
        _PURPOSES[model_ref]
        + " "
        + schema.get("description", "")
        + (" Submit once, then poll the returned operation ID and retrieve its result when completed.")
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    _describe(schema)
    examples = _examples(model_ref)
    if model_ref in _resource("native-examples.json"):
        refs += (_resource("native-examples.json")[model_ref]["source"],)
    if model_ref == "altumage":
        refs += ("k8s-inference/models/aging/fixtures.py#methylation_payload",)
    if model_ref == "genmol":
        refs += (
            "https://github.com/NVIDIA-BioNeMo/genmol/blob/"
            "add09fc83b7255bd09c797e527c0f4b51f5fb7c1/src/genmol/sampler.py",
        )
    if model_ref == "nv-segment-ct":
        refs += ("k8s-inference/catalog/runtime/validators/validate_nv_segment_ct.py",)
    return ModelInputContract(schema, examples, refs, model_ref, protocol)


def _examples(model_ref: str) -> tuple[dict[str, Any], ...]:
    if model_ref in _resource("paidf-chat.json"):
        return ({"model": _resource("paidf-chat.json")[model_ref]["properties"]["model"]["const"],
                 "messages": [{"role": "user", "content": "Describe the inputs accepted by this reference model."}],
                 "max_tokens": 64, "stream": False},)
    fixture = _resource("native-examples.json").get(model_ref)
    if fixture is not None:
        # The canonical validation requests retain their complete PDB bytes in
        # package data. Discovery must never inject those bytes into an LLM
        # context merely to explain how to call a model.
        request = copy.deepcopy(fixture["request"])
        if model_ref == "diffdock":
            request["protein"] = {"fixture_id": "pdb/1ubq"}
        elif model_ref == "proteinmpnn":
            request["input_pdb"] = {"fixture_id": "pdb/1ubq"}
        return (request,)
    if model_ref in {"qwen3-8b", "nv-reason-cxr-3b", "glm-5-2-fp8"}:
        return (
            {
                "messages": [{"role": "user", "content": "Explain what input this endpoint accepts."}],
                "max_completion_tokens": 64,
            },
        )
    examples: dict[str, dict[str, Any]] = {
        "boltz2": {
            "polymers": [
                {
                    "id": "A",
                    "molecule_type": "protein",
                    "sequence": _SEQUENCE,
                    "msa": {"msa_search": {"a3m": {"alignment": f">query\n{_SEQUENCE}\n"}}},
                }
            ]
        },
        "openfold2": {
            "input_id": "synthetic-protein",
            "sequence": _SEQUENCE,
            "selected_models": [1],
            "relax_prediction": False,
        },
        "openfold3": {
            "request_id": "synthetic-protein",
            "inputs": [
                {
                    "input_id": "protein",
                    "output_format": "cif",
                    "molecules": [{"id": "A", "type": "protein", "sequence": _SEQUENCE}],
                }
            ],
        },
        "genmol": {"smiles": "[*{20-30}]", "num_molecules": 1},
        "molmim": {"smi": "CC(=O)OC1=CC=CC=C1C(=O)O", "num_molecules": 1},
        "msa-search-pdb70": {"sequence": _SEQUENCE, "databases": ["pdb70_220313"], "output_alignment_formats": ["a3m"]},
        "sdxl": {"prompt": "A red cube on a white table", "seed": 1, "steps": 20, "response_format": "b64_json"},
        "cosmos3-nano": {
            "mode": "text-to-video",
            "prompt": "A red cube on a white table",
            "size": "448x256",
            "num_frames": 25,
            "fps": 24,
            "num_inference_steps": 30,
            "seed": 1,
        },
        "evo2-40b": {
            "sequence": "ATCGATCGATCG",
            "num_tokens": 20,
            "temperature": 0.7,
            "top_k": 1,
            "top_p": 0.0,
            "random_seed": 2407001,
            "enable_logits": False,
            "enable_sampled_probs": False,
            "enable_elapsed_ms_per_token": True,
        },
        "phenoage": {
            "samples": [
                {
                    "sample_id": "synthetic-clinical-0",
                    "age_years": 50,
                    "albumin_g_l": 45,
                    "creatinine_umol_l": 80,
                    "glucose_mmol_l": 5,
                    "c_reactive_protein_mg_dl": 0.1,
                    "lymphocyte_percent": 30,
                    "mean_cell_volume_fl": 90,
                    "red_cell_distribution_width_percent": 13,
                    "alkaline_phosphatase_u_l": 70,
                    "white_blood_cell_count_10e3_per_ul": 6,
                }
            ]
        },
        "altumage": {
            "cpg_sites": {
                "artifact_id": "00000000-0000-4000-8000-000000000001",
                "sha256": "0037a70f092cc2e157d07253a1849ff5d5035c6b60d6b7fb518f20ebc9e8f15e",
                "size_bytes": 284453,
                "media_type": "application/json",
                "compression": "none",
            },
            "missing_values": "error",
            "samples": [
                {
                    "sample_id": "synthetic-dnam-0",
                    "beta_values": {
                        "artifact_id": "00000000-0000-4000-8000-000000000002",
                        "sha256": "92051e78b8a8ef95f8b6d697c61725a318019906086aa40ecffe16467be725c0",
                        "size_bytes": 81273,
                        "media_type": "application/json",
                        "compression": "none",
                    },
                }
            ],
        },
        "nv-segment-ct": {
            "input_nifti_base64": {"fixture_id": "nifti/nv-segment-ct-synthetic-ellipsoid-v1"},
            "label_prompt": [1],
        },
        "cellpose-cpsam-v2": {
            "image_base64": {
                "artifact_id": "00000000-0000-4000-8000-000000000021",
                "sha256": "4" * 64,
                "size_bytes": 4096,
                "media_type": "image/png",
                "compression": "none",
            },
            "media_type": "image/png",
            "diameter": None,
            "research_only": True,
        },
        "scvi-scanvi": {
            "anndata_base64": {
                "artifact_id": "00000000-0000-4000-8000-000000000022",
                "sha256": "5" * 64,
                "size_bytes": 1048576,
                "media_type": "application/x-hdf5",
                "compression": "none",
            },
            "filename": "cells.h5ad",
            "method": "scvi",
            "batch_key": "batch",
            "max_epochs": 20,
            "n_latent": 10,
            "seed": 0,
            "research_only": True,
        },
        "wan2-2-t2v-nim": {
            "prompt": (
                "A colorful protein ribbon rotates slowly in a clean scientific visualization, smooth camera orbit"
            ),
            "size": "832x480",
            "seconds": 4,
            "seed": 7,
            "steps": 50,
            "cfg_scale": 5,
        },
        "wan2-2-i2v-nim": {
            "prompt": "Animate the scientific visualization with a slow cinematic orbit and subtle depth",
            "input_reference": {
                "artifact_id": "00000000-0000-4000-8000-000000000031",
                "sha256": "6" * 64,
                "size_bytes": 524288,
                "media_type": "image/png",
                "compression": "none",
            },
            "size": "832x480",
            "seconds": 4,
            "seed": 7,
        },
        "ace-step-1-5": {
            "prompt": "Instrumental cinematic electronic music for a scientific product demo, precise and optimistic",
            "lyrics": "[Instrumental]",
            "duration_seconds": 20,
            "thinking": True,
            "seed": 7,
        },
        "cosmos-transfer2-5-2b": {
            "video": {
                "artifact_id": "00000000-0000-4000-8000-000000000033",
                "sha256": "8" * 64,
                "size_bytes": 2097152,
                "media_type": "video/mp4",
                "compression": "none",
            },
            "prompt": "Preserve the camera and all recorded motion; change the weather to overcast.",
            "seed": 42,
            "num_steps": 35,
            "guidance": 7,
            "control_weight": 1.0,
            "output_delivery": "artifact",
        },
        "sam2-1-hiera-large": {
            "mode": "prompted-image",
            "media_base64": {
                "artifact_id": "00000000-0000-4000-8000-000000000032",
                "sha256": "7" * 64,
                "size_bytes": 524288,
                "media_type": "image/png",
                "compression": "none",
            },
            "media_type": "image/png",
            "points": [{"x": 512, "y": 384, "label": 1, "object_id": 1}],
        },
    }
    # Asset-bearing examples use immutable artifact or packaged fixture
    # references, never fabricated scientific bytes in the model context.
    return (examples[model_ref],) if model_ref in examples else ()


def packaged_input_fixture(fixture_id: str) -> tuple[bytes, str]:
    """Resolve a reviewed, immutable fixture without publishing its bytes."""

    if fixture_id == "pdb/1ubq":
        protein = _resource("native-examples.json")["diffdock"]["request"]["protein"]
        return str(protein).encode("utf-8"), "chemical/x-pdb"
    if fixture_id == "nifti/nv-segment-ct-synthetic-ellipsoid-v1":
        shape = (96, 96, 96)
        center = (48, 48, 48)
        axes = (24, 18, 12)
        voxels = array.array("f", [-1000.0]) * (shape[0] * shape[1] * shape[2])
        for z in range(shape[2]):
            dz = ((z - center[2]) / axes[2]) ** 2
            for y in range(shape[1]):
                dyz = ((y - center[1]) / axes[1]) ** 2 + dz
                for x in range(shape[0]):
                    if ((x - center[0]) / axes[0]) ** 2 + dyz <= 1.0:
                        voxels[x + shape[0] * (y + shape[1] * z)] = 100.0
        header = bytearray(352)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, *shape, 1, 1, 1, 1)
        struct.pack_into("<h", header, 70, 16)
        struct.pack_into("<h", header, 72, 32)
        struct.pack_into("<8f", header, 76, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        struct.pack_into("<f", header, 108, 352.0)
        struct.pack_into("<f", header, 112, 1.0)
        header[123] = 2
        struct.pack_into("<h", header, 254, 1)
        struct.pack_into("<4f", header, 280, 1.0, 0.0, 0.0, 0.0)
        struct.pack_into("<4f", header, 296, 0.0, 1.0, 0.0, 0.0)
        struct.pack_into("<4f", header, 312, 0.0, 0.0, 1.0, 0.0)
        header[344:348] = b"n+1\x00"
        return gzip.compress(bytes(header) + voxels.tobytes(), compresslevel=9, mtime=0), "application/gzip"
    raise KeyError(fixture_id)


def _rebase_refs(value: Any, prefix: str) -> Any:
    if isinstance(value, dict):
        return {
            key: prefix + child[1:]
            if key == "$ref" and isinstance(child, str) and child.startswith("#/")
            else _rebase_refs(child, prefix)
            for key, child in value.items()
            if key not in {"$id", "$schema"}
        }
    if isinstance(value, list):
        return [_rebase_refs(child, prefix) for child in value]
    return value


def scientific_contract_for(
    profile: ScientificWorkloadProfile,
    *,
    catalog: ScientificProfileCatalog,
) -> ModelInputContract:
    """Inline the exact selected profile schema into the canonical run envelope."""
    try:
        schema = copy.deepcopy(dict(catalog._validators[SCIENTIFIC_REQUEST_SCHEMA].schema))
        parameters = dict(catalog._validators[profile.parameter_schema].schema)
    except (KeyError, AttributeError) as error:
        raise InputContractUnavailable(f"Canonical scientific schema unavailable for {profile.model_id}") from error
    schema.pop("$id", None)
    schema["properties"]["parameters"] = _rebase_refs(parameters, "#/properties/parameters")
    schema["properties"]["operation"]["enum"] = list(profile.operations)
    schema["properties"]["service_class"]["enum"] = list(profile.service_classes)
    model_ref = str(profile.value["model_id"])
    purpose = _PURPOSES.get(model_ref, f"{profile.display_name}: {profile.operations}")
    schema["description"] = (
        purpose + " Upload/finalize the input manifest, submit this asynchronous run, then poll its operation. "
        "After publication, inspect validation results and download the result/artifact manifest and output files. "
        "Example artifact references describe source fixtures, not uploads available to the caller."
    )
    _describe(schema)
    if model_ref == "cosmos3-lerobot-augmentation":
        descriptions = {
            "source": (
                "LeRobot dataset source: an immutable uploaded zstd tar bundle, pinned Hugging Face revision, "
                "or authorized object-store binding; client-local paths are not accessible."
            ),
            "selection": (
                "Explicit episode indices and observation.images camera names to augment; unselected camera "
                "streams and recorded non-video fields are preserved."
            ),
            "variants": (
                "Number of augmented variants and exactly that many unique seeds, so each requested variation "
                "has a reproducible identity."
            ),
            "augmentation": (
                "Video-to-video continues selected prefix/suffix frames and may change future motion. Transfer "
                "uses full-sequence spatial controls. Neither guarantees recorded-action alignment or "
                "policy-training validity; review generated trajectories before use."
            ),
            "actions": (
                "Recorded action policy. Only preserve is supported: original actions and states remain unchanged; "
                "inverse-dynamics replacement actions are not qualified."
            ),
            "failure_policy": (
                "Whether an exhausted segment failure stops the run (fail-fast) or permits other segments to "
                "continue, and the bounded maximum attempts per segment."
            ),
        }
        for name, description in descriptions.items():
            schema["properties"]["parameters"]["properties"][name]["description"] = description
    record = _resource("scientific-examples.json").get(model_ref)
    refs: tuple[str, ...] = (SCIENTIFIC_REQUEST_SCHEMA, profile.parameter_schema)
    examples = () if record is None else (copy.deepcopy(record["request"]),)
    if record is not None:
        refs += (record["source"],)
    return ModelInputContract(schema, examples, refs, model_ref, "scientific-batch-v1")
