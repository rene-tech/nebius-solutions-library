#!/usr/bin/env python3
"""Production RFdiffusion runtime adapter.

Contract, in order:

1. Parse and bound every request parameter before anything expensive happens.
   Contigs and motif spans have a closed grammar, so nothing a caller supplies can
   become an extra Hydra override.
2. Resolve the Base checkpoint out of the content-addressed artifact plane and
   verify its sha256 against the request's declared artifact identity.
3. Execute upstream ``scripts/run_inference.py`` as a shell-free argv vector.
4. Verify the exact artifact markers upstream promises to write, confirm the run
   actually used a CUDA device, and check the designed backbone against what was
   requested.
5. Only then write a terminal-success result envelope.

Any failure before step 5 is terminal failure. The adapter never reports success
because a process exited 0; it reports success because the artifacts verified.

Upstream contract notes that shaped this adapter (RFdiffusion v1.1.0, commit
9273ef67335acaf91df0150473a274759229cdf6):

* There is no ``inference.seed``. ``run_inference.py`` calls
  ``make_deterministic(i_des)`` per design, so the per-design seed *is* the design
  index. A deterministic seed therefore maps to ``inference.design_startnum``
  together with ``inference.deterministic=True``. Reporting a seed we did not
  actually control would be a lie, so the envelope records the design index that
  seeded each design.
* Upstream writes ``torch.cuda.get_device_name(...)`` into each ``.trb``. That is
  upstream's own record of the device it ran on, so it is the CUDA evidence this
  adapter trusts rather than anything the adapter observes about itself.
* ``inference.cautious=True`` makes upstream silently skip a design whose ``.pdb``
  already exists. The adapter requires an empty output directory so a stale file
  can never be mistaken for a fresh design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_PARAMETERS = "fs2-serve.nebius.ai/rfdiffusion-parameters/v1"
SCHEMA_RESULT = "fs2-serve.nebius.ai/scientific-run-result/v1"
SCHEMA_REQUEST = "fs2-serve.nebius.ai/scientific-run-request/v1"
SCHEMA_MANIFEST = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"

ADAPTER_ID = "rfdiffusion-v1-1-0-base-v1"
MODEL_ID = "rfdiffusion"

OPERATION_DESIGN_BACKBONE = "design-backbone"
OPERATION_SCAFFOLD_MOTIF = "scaffold-motif"
OPERATIONS = (OPERATION_DESIGN_BACKBONE, OPERATION_SCAFFOLD_MOTIF)

# Bounds. These are runtime admission limits, not scientific limits: they exist so a
# single request cannot monopolise a GPU or smuggle a Hydra override through a contig.
MAX_CONTIG_GROUPS = 4
MAX_CONTIG_STRING_CHARS = 256
MAX_SEGMENTS_PER_GROUP = 32
MAX_TOTAL_RESIDUES = 512
MIN_TOTAL_RESIDUES = 1
MAX_NUM_DESIGNS = 64
MAX_SEED = 1_000_000
MIN_DIFFUSER_T = 1
MAX_DIFFUSER_T = 200
MAX_MOTIF_RESIDUE_INDEX = 99_999
MAX_HOTSPOT_RESIDUES = 64
DEFAULT_DIFFUSER_T = 50
DEFAULT_MOTIF_CA_RMSD_LIMIT = 1.5

# Closed grammar. A diffused span is "N-M"; a motif span is "<chain><start>-<end>";
# "0" is an explicit chain break. Nothing else is accepted, anywhere.
_RE_DIFFUSED_SPAN = re.compile(r"^(\d{1,5})-(\d{1,5})$")
_RE_MOTIF_SPAN = re.compile(r"^([A-Za-z])(\d{1,5})-(\d{1,5})$")
_RE_CHAIN_BREAK = re.compile(r"^0$")
_RE_HOTSPOT = re.compile(r"^([A-Za-z])(\d{1,5})$")

CACHE_LEVELS = (
    "cold-registry-pull",
    "image-local",
    "artifact-local",
    "image-and-artifact-local",
    "warm-process",
)

# Three-letter residue codes RFdiffusion can emit or read.
_STANDARD_RESIDUES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
}


class RequestError(ValueError):
    """The request is malformed or out of bounds. Terminal, not retryable."""


class VerificationError(RuntimeError):
    """Upstream ran but its artifacts did not satisfy the contract."""


# --------------------------------------------------------------------------------
# Parameter validation
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContigSegment:
    kind: str  # "diffused" | "motif" | "break"
    minimum: int = 0
    maximum: int = 0
    chain: str | None = None
    start: int = 0
    end: int = 0

    @property
    def motif_length(self) -> int:
        return self.end - self.start + 1 if self.kind == "motif" else 0


@dataclass(frozen=True)
class ContigGroup:
    raw: str
    segments: tuple[ContigSegment, ...]

    @property
    def min_length(self) -> int:
        return sum(s.minimum if s.kind == "diffused" else s.motif_length for s in self.segments)

    @property
    def max_length(self) -> int:
        return sum(s.maximum if s.kind == "diffused" else s.motif_length for s in self.segments)

    @property
    def is_fixed_length(self) -> bool:
        return self.min_length == self.max_length

    @property
    def motif_segments(self) -> tuple[ContigSegment, ...]:
        return tuple(s for s in self.segments if s.kind == "motif")


def _parse_contig_group(raw: object, index: int) -> ContigGroup:
    if not isinstance(raw, str):
        raise RequestError(f"contigs[{index}] must be a string, got {type(raw).__name__}")
    if not raw:
        raise RequestError(f"contigs[{index}] is empty")
    if len(raw) > MAX_CONTIG_STRING_CHARS:
        raise RequestError(
            f"contigs[{index}] is {len(raw)} characters, limit is {MAX_CONTIG_STRING_CHARS}"
        )

    pieces = raw.split("/")
    if len(pieces) > MAX_SEGMENTS_PER_GROUP:
        raise RequestError(
            f"contigs[{index}] has {len(pieces)} segments, limit is {MAX_SEGMENTS_PER_GROUP}"
        )

    segments: list[ContigSegment] = []
    for position, piece in enumerate(pieces):
        token = piece.strip()
        if token != piece:
            raise RequestError(
                f"contigs[{index}] segment {position} ('{piece}') has surrounding whitespace"
            )
        if _RE_CHAIN_BREAK.match(token):
            segments.append(ContigSegment(kind="break"))
            continue

        motif = _RE_MOTIF_SPAN.match(token)
        if motif:
            chain, start_text, end_text = motif.groups()
            start, end = int(start_text), int(end_text)
            if start < 1 or end < 1:
                raise RequestError(
                    f"contigs[{index}] motif '{token}' uses a residue index below 1"
                )
            if start > end:
                raise RequestError(
                    f"contigs[{index}] motif '{token}' is reversed ({start} > {end})"
                )
            if end > MAX_MOTIF_RESIDUE_INDEX:
                raise RequestError(
                    f"contigs[{index}] motif '{token}' exceeds residue index "
                    f"{MAX_MOTIF_RESIDUE_INDEX}"
                )
            segments.append(
                ContigSegment(kind="motif", chain=chain.upper(), start=start, end=end)
            )
            continue

        diffused = _RE_DIFFUSED_SPAN.match(token)
        if diffused:
            low, high = int(diffused.group(1)), int(diffused.group(2))
            if low > high:
                raise RequestError(
                    f"contigs[{index}] span '{token}' is reversed ({low} > {high})"
                )
            if low < 1:
                raise RequestError(f"contigs[{index}] span '{token}' has a zero-length minimum")
            segments.append(ContigSegment(kind="diffused", minimum=low, maximum=high))
            continue

        raise RequestError(
            f"contigs[{index}] segment '{token}' is not a diffused span (N-M), "
            "a motif span (<chain><start>-<end>) or a chain break (0)"
        )

    if not any(s.kind in {"diffused", "motif"} for s in segments):
        raise RequestError(f"contigs[{index}] contains no residues")
    return ContigGroup(raw=raw, segments=tuple(segments))


def _require_int(value: object, name: str, low: int, high: int, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise RequestError(f"{name} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{name} must be an integer, got {type(value).__name__}")
    if value < low or value > high:
        raise RequestError(f"{name} must be within [{low}, {high}], got {value}")
    return value


@dataclass(frozen=True)
class RFdiffusionParameters:
    operation: str
    contig_groups: tuple[ContigGroup, ...]
    num_designs: int
    seed: int
    diffuser_T: int
    length: int | None
    hotspot_residues: tuple[str, ...]
    input_pdb_artifact_id: str | None
    motif_ca_rmsd_limit: float

    @property
    def design_indices(self) -> tuple[int, ...]:
        return tuple(range(self.seed, self.seed + self.num_designs))

    @property
    def contig_literal(self) -> str:
        """The exact Hydra list literal, e.g. ``[100-100]``.

        Safe to build by concatenation only because every group already matched the
        closed grammar above.
        """
        return "[" + ",".join(group.raw for group in self.contig_groups) + "]"

    @property
    def total_min_residues(self) -> int:
        return sum(g.min_length for g in self.contig_groups)

    @property
    def total_max_residues(self) -> int:
        return sum(g.max_length for g in self.contig_groups)

    @property
    def motif_segments(self) -> tuple[ContigSegment, ...]:
        return tuple(s for g in self.contig_groups for s in g.motif_segments)


def parse_parameters(raw: object) -> RFdiffusionParameters:
    if not isinstance(raw, dict):
        raise RequestError("parameters must be an object")

    schema = raw.get("schema")
    if schema != SCHEMA_PARAMETERS:
        raise RequestError(f"parameters.schema must be {SCHEMA_PARAMETERS}, got {schema!r}")

    known = {
        "schema", "operation", "contigs", "num_designs", "seed", "diffuser_T",
        "length", "hotspot_residues", "input_pdb_artifact_id", "motif_ca_rmsd_limit",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise RequestError(f"parameters has unsupported keys: {', '.join(unknown)}")

    operation = raw.get("operation")
    if operation not in OPERATIONS:
        raise RequestError(f"operation must be one of {OPERATIONS}, got {operation!r}")

    contigs = raw.get("contigs")
    if not isinstance(contigs, list) or not contigs:
        raise RequestError("contigs must be a non-empty list of strings")
    if len(contigs) > MAX_CONTIG_GROUPS:
        raise RequestError(
            f"contigs has {len(contigs)} groups, limit is {MAX_CONTIG_GROUPS}"
        )
    groups = tuple(_parse_contig_group(item, i) for i, item in enumerate(contigs))

    total_max = sum(g.max_length for g in groups)
    total_min = sum(g.min_length for g in groups)
    if total_max > MAX_TOTAL_RESIDUES:
        raise RequestError(
            f"contigs request up to {total_max} residues, limit is {MAX_TOTAL_RESIDUES}"
        )
    if total_min < MIN_TOTAL_RESIDUES:
        raise RequestError("contigs request no residues")

    num_designs = _require_int(raw.get("num_designs"), "num_designs", 1, MAX_NUM_DESIGNS, 1)
    seed = _require_int(raw.get("seed"), "seed", 0, MAX_SEED, 0)
    if seed + num_designs - 1 > MAX_SEED:
        raise RequestError(
            f"seed {seed} plus num_designs {num_designs} exceeds the maximum design index {MAX_SEED}"
        )
    diffuser_t = _require_int(
        raw.get("diffuser_T"), "diffuser_T", MIN_DIFFUSER_T, MAX_DIFFUSER_T, DEFAULT_DIFFUSER_T
    )

    length = raw.get("length")
    if length is not None:
        length = _require_int(length, "length", MIN_TOTAL_RESIDUES, MAX_TOTAL_RESIDUES)

    hotspots_raw = raw.get("hotspot_residues") or []
    if not isinstance(hotspots_raw, list):
        raise RequestError("hotspot_residues must be a list")
    if len(hotspots_raw) > MAX_HOTSPOT_RESIDUES:
        raise RequestError(
            f"hotspot_residues has {len(hotspots_raw)} entries, limit is {MAX_HOTSPOT_RESIDUES}"
        )
    hotspots: list[str] = []
    for i, item in enumerate(hotspots_raw):
        if not isinstance(item, str) or not _RE_HOTSPOT.match(item):
            raise RequestError(
                f"hotspot_residues[{i}] must look like 'A123', got {item!r}"
            )
        hotspots.append(item.upper())

    motif_segments = [s for g in groups for s in g.motif_segments]
    input_pdb_artifact_id = raw.get("input_pdb_artifact_id")
    if input_pdb_artifact_id is not None and not isinstance(input_pdb_artifact_id, str):
        raise RequestError("input_pdb_artifact_id must be a string")

    if operation == OPERATION_SCAFFOLD_MOTIF:
        if not motif_segments:
            raise RequestError(
                "operation 'scaffold-motif' requires at least one motif span in contigs"
            )
        if not input_pdb_artifact_id:
            raise RequestError(
                "operation 'scaffold-motif' requires input_pdb_artifact_id"
            )
    else:
        if motif_segments:
            raise RequestError(
                "operation 'design-backbone' must not reference motif spans; "
                "use 'scaffold-motif'"
            )
        if input_pdb_artifact_id:
            raise RequestError(
                "operation 'design-backbone' must not supply input_pdb_artifact_id"
            )

    rmsd_limit = raw.get("motif_ca_rmsd_limit", DEFAULT_MOTIF_CA_RMSD_LIMIT)
    if isinstance(rmsd_limit, bool) or not isinstance(rmsd_limit, (int, float)):
        raise RequestError("motif_ca_rmsd_limit must be a number")
    if not (0 < float(rmsd_limit) <= 10.0):
        raise RequestError("motif_ca_rmsd_limit must be within (0, 10]")

    return RFdiffusionParameters(
        operation=operation,
        contig_groups=groups,
        num_designs=num_designs,
        seed=seed,
        diffuser_T=diffuser_t,
        length=length,
        hotspot_residues=tuple(hotspots),
        input_pdb_artifact_id=input_pdb_artifact_id,
        motif_ca_rmsd_limit=float(rmsd_limit),
    )


# --------------------------------------------------------------------------------
# Minimal PDB reader
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Residue:
    chain: str
    seq: int
    insertion_code: str
    name: str
    ca: tuple[float, float, float] | None


def parse_pdb_residues(path: Path) -> list[Residue]:
    """Read ATOM records. Fixed-column PDB, which is what upstream writes."""
    residues: dict[tuple[str, int, str], dict[str, Any]] = {}
    order: list[tuple[str, int, str]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            if not line.startswith("ATOM"):
                continue
            if len(line) < 54:
                raise VerificationError(
                    f"{path.name}: truncated ATOM record ({len(line)} chars)"
                )
            atom_name = line[12:16].strip()
            residue_name = line[17:20].strip()
            chain = line[21:22].strip() or " "
            try:
                seq = int(line[22:26])
            except ValueError as exc:
                raise VerificationError(
                    f"{path.name}: unparseable residue sequence number {line[22:26]!r}"
                ) from exc
            insertion_code = line[26:27].strip()
            key = (chain, seq, insertion_code)
            if key not in residues:
                residues[key] = {"name": residue_name, "ca": None}
                order.append(key)
            if atom_name == "CA":
                try:
                    coords = (
                        float(line[30:38]), float(line[38:46]), float(line[46:54])
                    )
                except ValueError as exc:
                    raise VerificationError(
                        f"{path.name}: unparseable CA coordinates for residue {key}"
                    ) from exc
                residues[key]["ca"] = coords

    return [
        Residue(chain=k[0], seq=k[1], insertion_code=k[2], name=residues[k]["name"], ca=residues[k]["ca"])
        for k in order
    ]


Vector = tuple[float, float, float]


def _rmsd(a: Sequence[Vector], b: Sequence[Vector]) -> float:
    """Plain coordinate RMSD, with no superposition."""
    if len(a) != len(b) or not a:
        raise VerificationError("cannot compute RMSD over mismatched or empty coordinate sets")
    total = 0.0
    for p, q in zip(a, b):
        total += (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
    return (total / len(a)) ** 0.5


def _centroid(points: Sequence[Vector]) -> Vector:
    n = float(len(points))
    return (
        sum(p[0] for p in points) / n,
        sum(p[1] for p in points) / n,
        sum(p[2] for p in points) / n,
    )


def _jacobi_eigensystem(
    matrix: list[list[float]], iterations: int = 100, tolerance: float = 1e-12
) -> tuple[list[float], list[list[float]]]:
    """Cyclic Jacobi eigendecomposition of a real symmetric matrix.

    Stdlib only, on purpose: the offline contract tests must run without numpy, and
    the matrix here is 4x4, so a direct Jacobi sweep is both exact enough and easier
    to audit than pulling in a linear-algebra dependency. Returns eigenvalues and
    eigenvectors, where eigenvectors[i] is the vector for eigenvalues[i].
    """
    size = len(matrix)
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]

    for _ in range(iterations):
        off = 0.0
        for i in range(size):
            for j in range(i + 1, size):
                off += a[i][j] * a[i][j]
        if off <= tolerance:
            break
        for p in range(size):
            for q in range(p + 1, size):
                if abs(a[p][q]) <= tolerance:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                sign = 1.0 if theta >= 0 else -1.0
                t = sign / (abs(theta) + (theta * theta + 1.0) ** 0.5)
                c = 1.0 / (t * t + 1.0) ** 0.5
                s = t * c
                for k in range(size):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(size):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                for k in range(size):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq

    eigenvalues = [a[i][i] for i in range(size)]
    eigenvectors = [[v[row][col] for row in range(size)] for col in range(size)]
    return eigenvalues, eigenvectors


def superpose(mobile: Sequence[Vector], reference: Sequence[Vector]) -> dict[str, Any]:
    """Optimally superpose ``mobile`` onto ``reference`` and report the residual.

    Horn's quaternion method: build the 3x3 correlation matrix of the centred point
    sets, form the 4x4 key matrix, and take the eigenvector of its largest eigenvalue
    as the optimal rotation quaternion.

    This exists because RFdiffusion emits designs in its own recentred frame. Comparing
    raw coordinates therefore measures where the design was *placed*, not whether the
    motif was *kept*: a perfectly scaffolded motif can sit tens of angstroms from the
    reference after a rigid-body move. Motif preservation is only meaningful after
    optimal superposition, and the rigid transform itself is reported so a reviewer can
    see how large a move was removed.
    """
    if len(mobile) != len(reference) or not mobile:
        raise VerificationError("cannot superpose mismatched or empty coordinate sets")

    mobile_centroid = _centroid(mobile)
    reference_centroid = _centroid(reference)
    m = [(p[0] - mobile_centroid[0], p[1] - mobile_centroid[1], p[2] - mobile_centroid[2]) for p in mobile]
    r = [
        (p[0] - reference_centroid[0], p[1] - reference_centroid[1], p[2] - reference_centroid[2])
        for p in reference
    ]

    s = [[sum(m[i][a] * r[i][b] for i in range(len(m))) for b in range(3)] for a in range(3)]
    sxx, sxy, sxz = s[0]
    syx, syy, syz = s[1]
    szx, szy, szz = s[2]

    key = [
        [sxx + syy + szz, syz - szy, szx - sxz, sxy - syx],
        [syz - szy, sxx - syy - szz, sxy + syx, szx + sxz],
        [szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy],
        [sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz],
    ]
    eigenvalues, eigenvectors = _jacobi_eigensystem(key)
    best = max(range(4), key=lambda i: eigenvalues[i])
    qw, qx, qy, qz = eigenvectors[best]
    norm = (qw * qw + qx * qx + qy * qy + qz * qz) ** 0.5
    if norm == 0.0:
        raise VerificationError("superposition produced a degenerate rotation")
    qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm

    rotation = [
        [
            qw * qw + qx * qx - qy * qy - qz * qz,
            2.0 * (qx * qy - qw * qz),
            2.0 * (qx * qz + qw * qy),
        ],
        [
            2.0 * (qx * qy + qw * qz),
            qw * qw - qx * qx + qy * qy - qz * qz,
            2.0 * (qy * qz - qw * qx),
        ],
        [
            2.0 * (qx * qz - qw * qy),
            2.0 * (qy * qz + qw * qx),
            qw * qw - qx * qx - qy * qy + qz * qz,
        ],
    ]

    aligned = [
        (
            rotation[0][0] * p[0] + rotation[0][1] * p[1] + rotation[0][2] * p[2] + reference_centroid[0],
            rotation[1][0] * p[0] + rotation[1][1] * p[1] + rotation[1][2] * p[2] + reference_centroid[1],
            rotation[2][0] * p[0] + rotation[2][1] * p[1] + rotation[2][2] * p[2] + reference_centroid[2],
        )
        for p in m
    ]

    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    cosine = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    rotation_degrees = math.degrees(math.acos(cosine))
    translation = math.dist(mobile_centroid, reference_centroid)

    return {
        "rmsd": _rmsd(aligned, reference),
        "rmsd_unaligned": _rmsd(mobile, reference),
        "rotation_degrees": rotation_degrees,
        "translation_angstrom": translation,
        "aligned": aligned,
    }


# --------------------------------------------------------------------------------
# Artifact plane
# --------------------------------------------------------------------------------


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResolvedArtifact:
    artifact_id: str
    path: Path
    sha256: str
    size_bytes: int
    verified: bool


def _manifest_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RequestError("input manifest has no 'entries' list")
    resolved: dict[str, dict[str, Any]] = {}
    for entry in entries:
        artifact = (entry or {}).get("artifact") or {}
        artifact_id = artifact.get("artifact_id")
        if not artifact_id:
            raise RequestError("input manifest entry is missing artifact.artifact_id")
        resolved[artifact_id] = artifact
    return resolved


def resolve_artifact(
    artifact_id: str,
    artifacts: dict[str, dict[str, Any]],
    artifact_root: Path,
    verify_digest: bool,
) -> ResolvedArtifact:
    declared = artifacts.get(artifact_id)
    if declared is None:
        raise RequestError(
            f"artifact '{artifact_id}' is not present in the input manifest"
        )
    relative = declared.get("path") or declared.get("relative_path")
    if not relative:
        raise RequestError(f"artifact '{artifact_id}' declares no path")
    if os.path.isabs(relative) or ".." in Path(relative).parts:
        raise RequestError(
            f"artifact '{artifact_id}' path must be relative and contained: {relative!r}"
        )
    path = (artifact_root / relative).resolve()
    root = artifact_root.resolve()
    if root not in path.parents and path != root:
        raise RequestError(
            f"artifact '{artifact_id}' resolves outside the artifact root: {path}"
        )
    if not path.is_file():
        raise RequestError(f"artifact '{artifact_id}' is not present at {path}")

    size = path.stat().st_size
    declared_size = declared.get("size_bytes")
    if isinstance(declared_size, int) and declared_size != size:
        raise RequestError(
            f"artifact '{artifact_id}' is {size} bytes, manifest declares {declared_size}"
        )

    declared_sha = (declared.get("sha256") or "").lower()
    if not declared_sha:
        raise RequestError(f"artifact '{artifact_id}' declares no sha256")

    actual_sha = declared_sha
    verified = False
    if verify_digest:
        actual_sha = sha256_file(path)
        if actual_sha != declared_sha:
            raise RequestError(
                f"artifact '{artifact_id}' sha256 mismatch\n"
                f"  manifest {declared_sha}\n  on disk  {actual_sha}"
            )
        verified = True

    return ResolvedArtifact(
        artifact_id=artifact_id, path=path, sha256=actual_sha, size_bytes=size, verified=verified
    )


# --------------------------------------------------------------------------------
# argv construction
# --------------------------------------------------------------------------------


def build_argv(
    parameters: RFdiffusionParameters,
    *,
    checkpoint: Path,
    output_prefix: Path,
    hydra_run_dir: Path,
    schedule_directory: Path,
    input_pdb: Path | None,
    upstream_home: Path,
    python_executable: str = sys.executable,
) -> list[str]:
    """Build the exact upstream argv. No shell, ever."""
    argv = [
        python_executable,
        str(upstream_home / "scripts" / "run_inference.py"),
        f"inference.output_prefix={output_prefix}",
        f"inference.ckpt_override_path={checkpoint}",
        f"inference.num_designs={parameters.num_designs}",
        f"inference.design_startnum={parameters.seed}",
        "inference.deterministic=True",
        f"diffuser.T={parameters.diffuser_T}",
        f"contigmap.contigs={parameters.contig_literal}",
        # Upstream caches its IGSO3 schedules beside its own package when this is
        # left unset, which is read-only here by design. Redirecting it to scratch
        # is the supported override; without it the run dies after loading the
        # checkpoint, before any diffusion.
        f"inference.schedule_directory_path={schedule_directory}",
        f"hydra.run.dir={hydra_run_dir}",
        "hydra.output_subdir=null",
    ]
    if parameters.length is not None:
        argv.append(f"contigmap.length={parameters.length}-{parameters.length}")
    if input_pdb is not None:
        argv.append(f"inference.input_pdb={input_pdb}")
    if parameters.hotspot_residues:
        argv.append(
            "ppi.hotspot_res=[" + ",".join(parameters.hotspot_residues) + "]"
        )
    return argv


# --------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------


@dataclass
class DesignVerification:
    design_index: int
    pdb_path: Path
    trb_path: Path
    residue_count: int
    chains: list[str]
    device: str
    upstream_seconds: float
    motif_positions: int = 0
    motif_fit: dict[str, Any] | None = None
    trajectory_paths: list[Path] = field(default_factory=list)


def _load_trb(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception as exc:  # noqa: BLE001 - any unpickling failure is terminal
        raise VerificationError(f"{path.name}: unreadable run metadata: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"{path.name}: run metadata is not a mapping")
    return payload


def _require_cuda_device(trb: dict[str, Any], path: Path) -> str:
    device = trb.get("device")
    if not isinstance(device, str) or not device.strip():
        raise VerificationError(f"{path.name}: run metadata records no device")
    if device.strip().upper() == "CPU":
        raise VerificationError(
            f"{path.name}: upstream recorded device 'CPU'. This runtime requires CUDA "
            "execution; a CPU fallback is a failed run, not a degraded one."
        )
    return device


def verify_design(
    design_index: int,
    output_prefix: Path,
    parameters: RFdiffusionParameters,
    reference_residues: list[Residue] | None,
) -> DesignVerification:
    pdb_path = Path(f"{output_prefix}_{design_index}.pdb")
    trb_path = Path(f"{output_prefix}_{design_index}.trb")

    for marker in (pdb_path, trb_path):
        if not marker.is_file():
            raise VerificationError(f"expected artifact marker is missing: {marker}")
        if marker.stat().st_size == 0:
            raise VerificationError(f"artifact marker is empty: {marker}")

    residues = parse_pdb_residues(pdb_path)
    if not residues:
        raise VerificationError(f"{pdb_path.name}: contains no ATOM records")

    missing_ca = [r for r in residues if r.ca is None]
    if missing_ca:
        raise VerificationError(
            f"{pdb_path.name}: {len(missing_ca)} residues have no CA atom"
        )
    unknown = sorted({r.name for r in residues} - _STANDARD_RESIDUES)
    if unknown:
        raise VerificationError(
            f"{pdb_path.name}: non-standard residue names present: {', '.join(unknown)}"
        )

    residue_count = len(residues)
    low, high = parameters.total_min_residues, parameters.total_max_residues
    if parameters.length is not None:
        low = high = parameters.length
    if not (low <= residue_count <= high):
        raise VerificationError(
            f"{pdb_path.name}: designed {residue_count} residues, "
            f"contigs requested {low}..{high}"
        )

    trb = _load_trb(trb_path)
    device = _require_cuda_device(trb, trb_path)
    upstream_seconds = float(trb.get("time") or 0.0)

    verification = DesignVerification(
        design_index=design_index,
        pdb_path=pdb_path,
        trb_path=trb_path,
        residue_count=residue_count,
        chains=sorted({r.chain for r in residues}),
        device=device,
        upstream_seconds=upstream_seconds,
    )

    trajectory_dir = output_prefix.parent / "traj"
    if trajectory_dir.is_dir():
        stem = f"{output_prefix.name}_{design_index}"
        verification.trajectory_paths = sorted(
            p for p in trajectory_dir.glob(f"{stem}_*_traj.pdb") if p.stat().st_size > 0
        )

    if parameters.operation == OPERATION_SCAFFOLD_MOTIF:
        verification.motif_positions, verification.motif_fit = _verify_motif(
            trb=trb,
            trb_path=trb_path,
            designed=residues,
            reference=reference_residues or [],
            parameters=parameters,
        )

    return verification


def _verify_motif(
    *,
    trb: dict[str, Any],
    trb_path: Path,
    designed: list[Residue],
    reference: list[Residue],
    parameters: RFdiffusionParameters,
) -> tuple[int, dict[str, Any]]:
    """Check the motif survived, using upstream's own contig mapping."""
    ref_idx = trb.get("con_ref_pdb_idx")
    hal_idx = trb.get("con_hal_pdb_idx")
    if not isinstance(ref_idx, (list, tuple)) or not isinstance(hal_idx, (list, tuple)):
        raise VerificationError(
            f"{trb_path.name}: run metadata carries no motif mapping "
            "(con_ref_pdb_idx / con_hal_pdb_idx)"
        )
    if len(ref_idx) != len(hal_idx):
        raise VerificationError(
            f"{trb_path.name}: motif mapping is inconsistent "
            f"({len(ref_idx)} reference vs {len(hal_idx)} designed positions)"
        )

    expected_positions = sum(s.motif_length for s in parameters.motif_segments)
    if len(ref_idx) != expected_positions:
        raise VerificationError(
            f"{trb_path.name}: motif mapping covers {len(ref_idx)} positions, "
            f"contigs requested {expected_positions}"
        )

    designed_by_key = {(r.chain, r.seq): r for r in designed}
    reference_by_key = {(r.chain, r.seq): r for r in reference}

    ref_coords: list[tuple[float, float, float]] = []
    designed_coords: list[tuple[float, float, float]] = []

    for position, (ref_entry, hal_entry) in enumerate(zip(ref_idx, hal_idx)):
        ref_key = (str(ref_entry[0]), int(ref_entry[1]))
        hal_key = (str(hal_entry[0]), int(hal_entry[1]))
        ref_residue = reference_by_key.get(ref_key)
        hal_residue = designed_by_key.get(hal_key)
        if ref_residue is None:
            raise VerificationError(
                f"motif position {position}: reference residue {ref_key} absent from the input PDB"
            )
        if hal_residue is None:
            raise VerificationError(
                f"motif position {position}: designed residue {hal_key} absent from the output PDB"
            )
        if ref_residue.name != hal_residue.name:
            raise VerificationError(
                f"motif position {position}: residue identity changed, "
                f"input {ref_key} is {ref_residue.name} but output {hal_key} is {hal_residue.name}"
            )
        if ref_residue.ca is None or hal_residue.ca is None:
            raise VerificationError(
                f"motif position {position}: missing CA atom in reference or design"
            )
        ref_coords.append(ref_residue.ca)
        designed_coords.append(hal_residue.ca)

    # Superpose before measuring. Upstream emits the design in its own recentred
    # frame, so raw coordinates measure placement, not motif preservation.
    fit = superpose(designed_coords, ref_coords)
    rmsd = fit["rmsd"]
    if rmsd > parameters.motif_ca_rmsd_limit:
        raise VerificationError(
            f"motif CA RMSD {rmsd:.3f} A after optimal superposition exceeds the limit "
            f"{parameters.motif_ca_rmsd_limit:.3f} A; the motif was not preserved "
            f"(unaligned {fit['rmsd_unaligned']:.3f} A, rigid-body move "
            f"{fit['rotation_degrees']:.1f} deg / {fit['translation_angstrom']:.3f} A)"
        )
    return len(ref_idx), fit


# --------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------


class PhaseTimer:
    """Wall-clock phase accounting, in the order the phases actually occurred."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._marks: list[tuple[str, float]] = []
        self.started_at = time.time()

    def mark(self, name: str) -> None:
        self._marks.append((name, time.monotonic()))

    def phases(self) -> dict[str, float]:
        out: dict[str, float] = {}
        previous = self._start
        for name, at in self._marks:
            out[name] = round(at - previous, 3)
            previous = at
        return out

    @property
    def total_seconds(self) -> float:
        return round(time.monotonic() - self._start, 3)


_RE_MAKING_DESIGN = re.compile(r"Making design ")
_RE_FINISHED = re.compile(r"Finished design in ([0-9.]+) minutes")


def run_upstream(
    argv: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, float | None, list[str]]:
    """Run upstream, tee its output, and note when the model became ready.

    The first ``Making design`` line is upstream's own signal that imports, weight
    load and sampler construction are done, so it separates model setup from
    diffusion without guessing.
    """
    first_design_at: float | None = None
    tail: list[str] = []
    started = time.monotonic()

    # Popen as a context manager closes the stdout pipe and reaps the child even when
    # the loop below raises, so a timeout or a verification failure cannot leak a file
    # descriptor into a long-lived process.
    with log_path.open("w", encoding="utf-8") as log_handle, subprocess.Popen(  # noqa: S603 - argv vector, shell=False
        list(argv),
        cwd=str(cwd),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        shell=False,
    ) as process:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                log_handle.write(line)
                log_handle.flush()
                sys.stdout.write(line)
                if first_design_at is None and _RE_MAKING_DESIGN.search(line):
                    first_design_at = time.monotonic() - started
                tail.append(line.rstrip("\n"))
                if len(tail) > 200:
                    tail.pop(0)
                if time.monotonic() - started > timeout_seconds:
                    process.kill()
                    raise VerificationError(
                        f"upstream exceeded the {timeout_seconds}s timeout"
                    )
            returncode = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)

    return returncode, first_design_at, tail


def _child_environment(upstream_home: Path, scratch: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(upstream_home),
            "DGLBACKEND": "pytorch",
            "HYDRA_FULL_ERROR": "1",
            "HOME": str(scratch),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "MPLCONFIGDIR": str(scratch / "matplotlib"),
            "DGL_HOME": str(scratch / "dgl"),
        }
    )
    return environment


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def command_probe(args: argparse.Namespace) -> int:
    """Readiness probe: can this image import the stack and see a GPU?"""
    report: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/runtime-probe/v1",
        "model_id": MODEL_ID,
        "adapter_id": ADAPTER_ID,
    }
    started = time.monotonic()
    try:
        import torch  # noqa: PLC0415 - deliberately lazy, this is the probe

        report["torch_version"] = torch.__version__
        report["torch_cuda_version"] = torch.version.cuda
        report["cuda_available"] = bool(torch.cuda.is_available())
        report["device_count"] = int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
        report["devices"] = [
            torch.cuda.get_device_name(i) for i in range(report["device_count"])
        ]
        report["torch_import_seconds"] = round(time.monotonic() - started, 3)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        report["error"] = f"torch unavailable: {exc}"
        print(json.dumps(report, indent=2))
        return 1

    upstream_home = Path(args.upstream_home)
    report["upstream_home"] = str(upstream_home)
    report["upstream_present"] = (upstream_home / "scripts" / "run_inference.py").is_file()

    try:
        import dgl  # noqa: PLC0415

        report["dgl_version"] = dgl.__version__
    except Exception as exc:  # noqa: BLE001
        report["dgl_error"] = str(exc)

    try:
        import se3_transformer  # noqa: PLC0415,F401

        report["se3_transformer"] = "importable"
    except Exception as exc:  # noqa: BLE001
        report["se3_transformer_error"] = str(exc)

    report["ready"] = bool(
        report.get("upstream_present")
        and report.get("cuda_available")
        and "dgl_error" not in report
        and "se3_transformer_error" not in report
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] or args.allow_cpu else 1


def _load_json(path: Path, expected_schema: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RequestError(f"{path}: unreadable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestError(f"{path}: expected a JSON object")
    if expected_schema and payload.get("schema") != expected_schema:
        raise RequestError(
            f"{path}: schema must be {expected_schema}, got {payload.get('schema')!r}"
        )
    return payload


def command_run(args: argparse.Namespace) -> int:
    timer = PhaseTimer()
    output_dir = Path(args.output).resolve()
    result_path = output_dir / "result.json"

    request_path = Path(args.request)
    manifest_path = Path(args.input_manifest)
    artifact_root = Path(args.artifact_root).resolve()
    upstream_home = Path(args.upstream_home).resolve()

    envelope: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "model_id": MODEL_ID,
        "adapter_id": ADAPTER_ID,
        "status": "failed",
        "started_at_epoch": timer.started_at,
    }

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        existing = [p for p in output_dir.iterdir() if p.name != "result.json"]
        if existing:
            raise RequestError(
                "output directory must be empty: upstream runs with cautious=True and "
                "would silently skip designs whose .pdb already exists. Found: "
                + ", ".join(sorted(p.name for p in existing)[:10])
            )

        request = _load_json(request_path, SCHEMA_REQUEST)
        manifest = _load_json(manifest_path, SCHEMA_MANIFEST)
        parameters = parse_parameters(request.get("parameters"))
        artifacts = _manifest_entries(manifest)

        envelope["operation"] = parameters.operation
        envelope["request"] = {
            "operation": parameters.operation,
            "contigs": [g.raw for g in parameters.contig_groups],
            "num_designs": parameters.num_designs,
            "seed": parameters.seed,
            "diffuser_T": parameters.diffuser_T,
            "length": parameters.length,
            "hotspot_residues": list(parameters.hotspot_residues),
            "requested_residues": {
                "minimum": parameters.total_min_residues,
                "maximum": parameters.total_max_residues,
            },
        }
        timer.mark("validate_request")

        checkpoint = resolve_artifact(
            args.checkpoint_artifact_id, artifacts, artifact_root,
            verify_digest=not args.skip_checkpoint_digest,
        )
        envelope["checkpoint"] = {
            "artifact_id": checkpoint.artifact_id,
            "path": str(checkpoint.path),
            "sha256": checkpoint.sha256,
            "size_bytes": checkpoint.size_bytes,
            "digest_verified": checkpoint.verified,
        }
        timer.mark("resolve_checkpoint")

        reference_residues: list[Residue] | None = None
        input_pdb: Path | None = None
        if parameters.input_pdb_artifact_id:
            reference = resolve_artifact(
                parameters.input_pdb_artifact_id, artifacts, artifact_root, verify_digest=True
            )
            input_pdb = reference.path
            reference_residues = parse_pdb_residues(input_pdb)
            envelope["input_pdb"] = {
                "artifact_id": reference.artifact_id,
                "sha256": reference.sha256,
                "size_bytes": reference.size_bytes,
                "residue_count": len(reference_residues),
            }
        timer.mark("resolve_inputs")

        scratch = Path(args.scratch).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        hydra_run_dir = scratch / "hydra"
        hydra_run_dir.mkdir(parents=True, exist_ok=True)
        schedule_directory = scratch / "schedules"
        schedule_directory.mkdir(parents=True, exist_ok=True)
        designs_dir = output_dir / "designs"
        designs_dir.mkdir(parents=True, exist_ok=True)
        output_prefix = designs_dir / "design"

        argv = build_argv(
            parameters,
            checkpoint=checkpoint.path,
            output_prefix=output_prefix,
            hydra_run_dir=hydra_run_dir,
            schedule_directory=schedule_directory,
            input_pdb=input_pdb,
            upstream_home=upstream_home,
        )
        envelope["upstream_argv"] = list(argv)
        envelope["shell_free"] = True

        log_path = output_dir / "upstream.log"
        returncode, first_design_at, tail = run_upstream(
            argv,
            cwd=upstream_home,
            log_path=log_path,
            environment=_child_environment(upstream_home, scratch),
            timeout_seconds=args.timeout_seconds,
        )
        timer.mark("upstream_execute")

        envelope["upstream"] = {
            "returncode": returncode,
            "log_path": str(log_path),
            "model_ready_seconds": round(first_design_at, 3) if first_design_at else None,
        }
        if returncode != 0:
            envelope["upstream"]["tail"] = tail[-40:]
            raise VerificationError(f"upstream exited {returncode}")

        verifications = [
            verify_design(index, output_prefix, parameters, reference_residues)
            for index in parameters.design_indices
        ]
        timer.mark("verify_artifacts")

        devices = sorted({v.device for v in verifications})
        envelope["designs"] = [
            {
                "design_index": v.design_index,
                "seed": v.design_index,
                "pdb": {
                    "path": str(v.pdb_path.relative_to(output_dir)),
                    "sha256": sha256_file(v.pdb_path),
                    "size_bytes": v.pdb_path.stat().st_size,
                },
                "run_metadata": {
                    "path": str(v.trb_path.relative_to(output_dir)),
                    "sha256": sha256_file(v.trb_path),
                    "size_bytes": v.trb_path.stat().st_size,
                },
                "residue_count": v.residue_count,
                "chains": v.chains,
                "device": v.device,
                "upstream_seconds": round(v.upstream_seconds, 3),
                "motif_positions_preserved": v.motif_positions or None,
                "motif_ca_rmsd_angstrom": (
                    round(v.motif_fit["rmsd"], 4) if v.motif_fit else None
                ),
                "motif_superposition": (
                    {
                        "method": "horn-quaternion-optimal-superposition",
                        "rmsd_angstrom": round(v.motif_fit["rmsd"], 4),
                        "rmsd_unaligned_angstrom": round(v.motif_fit["rmsd_unaligned"], 4),
                        "rigid_body_rotation_degrees": round(v.motif_fit["rotation_degrees"], 3),
                        "rigid_body_translation_angstrom": round(v.motif_fit["translation_angstrom"], 4),
                        "note": (
                            "Upstream emits designs in its own recentred frame. The unaligned "
                            "value measures placement, not motif preservation; only the "
                            "superposed value is compared against the limit."
                        ),
                    }
                    if v.motif_fit
                    else None
                ),
                "trajectory_files": [
                    str(p.relative_to(output_dir)) for p in v.trajectory_paths
                ],
            }
            for v in verifications
        ]
        envelope["accelerator"] = {
            "devices": devices,
            "cuda_execution_confirmed": True,
            "evidence": "upstream .trb run metadata records torch.cuda.get_device_name()",
        }
        envelope["cache_level"] = {
            "declared": args.cache_level,
            "source": "submitter-declared",
            "note": (
                "Declared by the submitting plan from observed image/artifact residency. "
                "This is an image and filesystem cache level only. No GPU memory snapshot "
                "or CRIU restore is used by this runtime."
            ),
            "gpu_snapshot_used": False,
        }
        timer.mark("write_envelope")

        envelope["status"] = "succeeded"
        envelope["phases_seconds"] = timer.phases()
        envelope["total_seconds"] = timer.total_seconds
        return 0

    except (RequestError, VerificationError) as exc:
        envelope["status"] = "failed"
        envelope["error"] = {"type": type(exc).__name__, "message": str(exc)}
        envelope["phases_seconds"] = timer.phases()
        envelope["total_seconds"] = timer.total_seconds
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - still must leave a truthful envelope
        envelope["status"] = "failed"
        envelope["error"] = {"type": type(exc).__name__, "message": str(exc)}
        envelope["phases_seconds"] = timer.phases()
        envelope["total_seconds"] = timer.total_seconds
        print(f"FAILED: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
            print(f"result envelope: {result_path} status={envelope['status']}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not write result envelope: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rfdiffusion-runtime", description="fs2 RFdiffusion production runtime adapter"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run a validated RFdiffusion request")
    run.add_argument("--request", required=True)
    run.add_argument("--input-manifest", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--artifact-root", default=os.environ.get("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts"))
    run.add_argument("--upstream-home", default=os.environ.get("FS2_RFDIFFUSION_HOME", "/opt/rfdiffusion"))
    run.add_argument("--checkpoint-artifact-id", default="artifact.rfdiffusion.base-ckpt")
    run.add_argument("--scratch", default="/tmp/fs2-rfdiffusion")
    run.add_argument("--timeout-seconds", type=int, default=3600)
    run.add_argument("--cache-level", choices=CACHE_LEVELS, default="cold-registry-pull")
    run.add_argument(
        "--skip-checkpoint-digest",
        action="store_true",
        help="skip re-hashing the checkpoint; only for repeated runs against an "
             "already-verified artifact generation",
    )
    run.set_defaults(func=command_run)

    probe = subparsers.add_parser("probe", help="readiness probe")
    probe.add_argument("--upstream-home", default=os.environ.get("FS2_RFDIFFUSION_HOME", "/opt/rfdiffusion"))
    probe.add_argument("--allow-cpu", action="store_true")
    probe.set_defaults(func=command_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
