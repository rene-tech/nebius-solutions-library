"""Source-backed input contracts for the currently hosted runtime adapters.

These describe public *payloads*, not admission envelopes or NVIDIA's larger
NIM interfaces. They do not enable routes. Scientific schemas are composed from
the same loaded catalog validators used by ScientificBatchService; runtime
Pydantic snapshots are checked against their source DTOs in the offline tests.
JSON Schema describes structure; the original runtime still checks biological
content, asset identities and cross-field relationships. No values are dropped.
"""

from __future__ import annotations

import copy
import json
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
                "string", "Caller-owned immutable artifact UUID returned after upload finalization.",
                pattern=_ARTIFACT_ID_PATTERN,
            ),
            "sha256": _field("string", "SHA-256 of the exact uploaded bytes.", pattern=_SHA256_PATTERN),
            "size_bytes": _field("integer", "Exact uploaded byte count.", minimum=0),
            "media_type": _field(
                "string", "Media type of the uploaded bytes.", enum=list(media_types),
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
                "string", "Pinned server-side smoke fixture; no fixture bytes pass through the language model.",
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
    "boltz2": "Predict proteins from chains and supplied A3M; returns mmCIF structures and confidence scores.",
    "openfold2": "Predict a protein from its sequence; returns ranked structure and confidence values.",
    "openfold3": "Predict a protein assembly from chains and optional supplied A3M; returns CIF structure results.",
    "diffdock": "Dock a SMILES ligand into a receptor PDB; returns ranked poses and docking confidence.",
    "proteinmpnn": "Design sequences compatible with a protein backbone PDB; returns sampled sequences and scores.",
    "genmol": "Generate de-novo molecules from a length mask; returns generated SMILES and property scores.",
    "molmim": "Optimize a starting SMILES molecule with CMA-ES; returns molecules with QED and similarity scores.",
    "msa-search-pdb70": "Search the pinned local PDB70 database for a protein sequence; returns A3M alignments.",
    "sdxl": "Generate a 512x512 image from text; returns PNG bytes or the selected JSON/base64 envelope.",
    "nv-segment-ct": "Segment a CT NIfTI volume from labels or points; returns encoded segmentation and label counts.",
    "cosmos3-nano": "Generate an image or video from text; returns a JSON/base64 PNG or MP4 with media metadata.",
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
        props["scoring"]["pattern"] = r"^(?:[Qq][Ee][Dd]|[Ll][Oo][Gg][Pp])$"
        props["temperature"]["anyOf"][0]["exclusiveMinimum"] = 0
        props["noise"]["anyOf"][0]["minimum"] = 0
        schema["description"] = (
            "GenMol de-novo mask generation. Range order and finite numeric strings are checked by the runtime."
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


def _cosmos() -> Schema:
    record = _resource("cosmos.json")
    image, video = copy.deepcopy(record["image"]), copy.deepcopy(record["video"])
    props = {**image["properties"], **video["properties"]}
    props["mode"] = _field("string", "Select image or video generation.", enum=["text-to-image", "text-to-video"])
    props["size"] = _field(
        "string",
        "WIDTHxHEIGHT: multiples of 16, width 256–1280, height 256–720, <=921600 pixels. "
        "Default is 512x512 for images and 448x256 for video; dimensions are validated by the runtime.",
        pattern=r"^[0-9]+x[0-9]+$",
        minLength=7,
        maxLength=9,
    )
    props["output_format"] = _field(
        "string", "png for images, mp4 for video (mode-specific default).", enum=["png", "mp4"]
    )
    props["model"]["description"] = "Optional fixed upstream identity; omit it to use the pinned Cosmos model."
    props["prompt"]["description"] = "Text description of the requested image or video."
    props["negative_prompt"]["description"] = "Unwanted image/video content."
    props["num_inference_steps"]["description"] = "Denoising steps."
    props["guidance_scale"]["description"] = "Prompt guidance strength."
    props["num_frames"]["description"] = "Number of video frames; valid only for text-to-video."
    props["fps"]["description"] = "Video frames per second; valid only for text-to-video."
    schema = _object(
        props, ("prompt", "mode"), "Pinned Cosmos3 Nano image/video adapter, not an arbitrary vLLM Omni API."
    )
    # Root stays an explicit object so admission controls may be added without
    # colliding with additionalProperties:false inside union branches.
    schema["allOf"] = [
        {
            "if": {"properties": {"mode": {"const": "text-to-image"}}, "required": ["mode"]},
            "then": {
                "properties": {"output_format": {"const": "png"}},
                "not": {"anyOf": [{"required": ["num_frames"]}, {"required": ["fps"]}]},
            },
            "else": {"properties": {"output_format": {"const": "mp4"}}},
        }
    ]
    return schema


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
    "openfold2": (_openfold2, "k8s-inference/models/structure/openfold2-upstream/server.py"),
    "openfold3": (_openfold3, "k8s-inference/models/structure/openfold3-preview2/server.py"),
    "diffdock": (_diffdock, "k8s-inference/models/structure/runtime/adapters/diffdock.py"),
    "proteinmpnn": (_proteinmpnn, "k8s-inference/models/structure/runtime/adapters/proteinmpnn.py"),
    "sdxl": (_sdxl, "k8s-inference/models/general-media/sdxl_server.py"),
    "nv-segment-ct": (_segment, "k8s-inference/models/general-media/nv_segment_ct_server.py"),
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


def contract_for(model: OperationalModel, protocol: str) -> ModelInputContract:
    """Resolve clones by source identity, without weakening route admission."""
    model_ref = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    _check_adapter(model, model_ref)
    if protocol not in model.gateway.protocols:
        raise InputContractUnavailable(f"{model_ref} does not publish protocol {protocol}")
    if protocol == "native" and model_ref in _resource("runtime-pydantic.json"):
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
    if model_ref == "nv-segment-ct":
        refs += ("k8s-inference/catalog/runtime/validators/validate_nv_segment_ct.py",)
    return ModelInputContract(schema, examples, refs, model_ref, protocol)


def _examples(model_ref: str) -> tuple[dict[str, Any], ...]:
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
    }
    # Asset-bearing examples must not fabricate canonical CpG labels, PDB or NIfTI.
    return (examples[model_ref],) if model_ref in examples else ()


def packaged_input_fixture(fixture_id: str) -> tuple[bytes, str]:
    """Resolve a reviewed, immutable fixture without publishing its bytes."""

    if fixture_id != "pdb/1ubq":
        raise KeyError(fixture_id)
    protein = _resource("native-examples.json")["diffdock"]["request"]["protein"]
    return str(protein).encode("utf-8"), "chemical/x-pdb"


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
    record = _resource("scientific-examples.json").get(model_ref)
    refs: tuple[str, ...] = (SCIENTIFIC_REQUEST_SCHEMA, profile.parameter_schema)
    examples = () if record is None else (copy.deepcopy(record["request"]),)
    if record is not None:
        refs += (record["source"],)
    return ModelInputContract(schema, examples, refs, model_ref, "scientific-batch-v1")
