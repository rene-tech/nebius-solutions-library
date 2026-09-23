"""Independent geometry test of a small mass-weighted SHAKE setup projection.

This is a diagnostic, not an integrator and not a raw-trajectory repair. The
fixed original constraint directions implement the position-only correction
in the pinned native SHAKE setup. No dynamics or velocities are advanced.
"""
import hashlib
import json
from pathlib import Path

import numpy as np

from geometry import ValidationError, minimum_image

KIND = "exact-lammps-shake-setup-projection-v1"
SOURCE_REVISION = "c7ae612a9497437412cb787b78769570f48653dd"
WORKER_IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c"


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_policy(policy, image, trajectories, topology):
    """Opt-in evidence binds the exact native image, files, topology and step."""
    if policy.get("kind") != KIND or image != WORKER_IMAGE:
        raise ValidationError("SHAKE setup exception is limited to the qualified exact LAMMPS image")
    path = Path(policy["evidence_path"])
    if file_hash(path) != policy["evidence_sha256"]:
        raise ValidationError("SHAKE setup evidence hash mismatch")
    evidence = json.loads(path.read_text())
    if evidence.get("source_revision") != SOURCE_REVISION or evidence.get("worker_image") != image:
        raise ValidationError("SHAKE setup source/image binding differs")
    if file_hash(topology) != evidence["topology_sha256"]:
        raise ValidationError("SHAKE setup topology binding differs")
    expected = evidence["boundary_files"]
    if len(trajectories) != 2 or [file_hash(p) for p in trajectories] != [expected[k]["sha256"] for k in ("previous", "next")]:
        raise ValidationError("SHAKE setup native segment bindings differ")
    model = evidence["model"]
    masses = np.asarray(model["masses_Da"])
    pairs = np.asarray(model["pairs_zero_based"], dtype=int)
    distances = np.asarray(model["distances_A"])
    if masses.shape != (6598,) or pairs.shape != (6588, 2) or distances.shape != (6588,) or np.any(pairs < 0) or np.any(pairs >= len(masses)) or len(np.unique(np.sort(pairs, axis=1), axis=0)) != len(pairs):
        raise ValidationError("SHAKE evidence is not the qualified canonical constraint model")
    return {"kind": KIND, "evidence_sha256": policy["evidence_sha256"], "step": evidence["metrics"]["boundary_step"], "model": model, "source_revision": SOURCE_REVISION}


def clusters(natoms, pairs):
    parent = list(range(natoms))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in pairs:
        parent[root(int(a))] = root(int(b))
    groups = {}
    for edge, (a, b) in enumerate(pairs):
        groups.setdefault(root(int(a)), []).append(edge)
    return [(np.unique(pairs[edges]), np.array(edges)) for edges in groups.values()]


def project_cluster(x, masses, pairs, distances):
    """Solve the small position-only SHAKE equations with fixed old directions."""
    n, k = len(x), len(pairs)
    jacobian = np.zeros((k, n, 3))
    for i, (a, b) in enumerate(pairs):
        jacobian[i, a] = x[a] - x[b]
        jacobian[i, b] = -jacobian[i, a]
    basis = jacobian.transpose(1, 2, 0) / masses[:, None, None]
    multipliers = np.zeros(k)
    for _ in range(12):
        delta = basis @ multipliers
        q = x + delta
        vectors = q[pairs[:, 0]] - q[pairs[:, 1]]
        residual = np.sum(vectors * vectors, axis=1) - distances**2
        if np.abs(residual).max() < 2e-14:
            return delta
        derivative = 2 * np.einsum("ij,ijk->ik", vectors, basis[pairs[:, 0]] - basis[pairs[:, 1]])
        multipliers -= np.linalg.solve(derivative, residual)
    raise ValidationError("small SHAKE setup projection did not converge")


def projection_metrics(before, after, cell, masses, pairs, distances):
    before, after, masses, pairs, distances = map(np.asarray, (before, after, masses, pairs, distances))
    if before.shape != after.shape or before.shape != (len(masses), 3) or len(pairs) != len(distances):
        raise ValidationError("SHAKE diagnostic dimensions differ")
    if not all(np.isfinite(x).all() for x in (before, after, cell, masses, distances)) or np.any(masses <= 0):
        raise ValidationError("nonfinite or nonpositive SHAKE diagnostic input")
    delta = minimum_image(after - before, cell)
    predicted = np.zeros_like(delta)
    com = []
    groups = clusters(len(before), pairs)
    for atoms, edges in groups:
        if not 2 <= len(atoms) <= 4:
            raise ValidationError("outside supported canonical SHAKE cluster sizes")
        local = np.searchsorted(atoms, pairs[edges])
        whole = minimum_image(before[atoms] - before[atoms[0]], cell)
        predicted[atoms] = project_cluster(whole, masses[atoms], local, distances[edges])
        com.append(np.sum(masses[atoms, None] * delta[atoms], axis=0) / masses[atoms].sum())
    involved = np.unique(pairs)
    free = np.setdiff1d(np.arange(len(before)), involved)
    errors = []
    for positions in (before, after):
        vectors = minimum_image(positions[pairs[:, 0]] - positions[pairs[:, 1]], cell)
        errors.append(np.linalg.norm(vectors, axis=1) - distances)
    return {
        "atoms": len(before), "constraints": len(pairs), "clusters": len(groups),
        "unconstrained_atoms": (free + 1).tolist(),
        "maximum_periodic_component_displacement_A": float(np.abs(delta).max()),
        "maximum_periodic_atom_displacement_A": float(np.linalg.norm(delta, axis=1).max()),
        "rms_component_displacement_A": float(np.sqrt(np.mean(delta**2))),
        "maximum_unconstrained_atom_component_displacement_A": float(np.abs(delta[free]).max()) if len(free) else 0.,
        "maximum_cluster_COM_component_displacement_A": float(np.abs(com).max()),
        "maximum_component_difference_from_position_only_SHAKE_solution_A": float(np.abs(delta - predicted).max()),
        "rms_component_difference_from_position_only_SHAKE_solution_A": float(np.sqrt(np.mean((delta - predicted)**2))),
        "maximum_constraint_distance_error_before_A": float(np.abs(errors[0]).max()),
        "maximum_constraint_distance_error_after_A": float(np.abs(errors[1]).max()),
        "rms_constraint_distance_error_before_A": float(np.sqrt(np.mean(errors[0]**2))),
        "rms_constraint_distance_error_after_A": float(np.sqrt(np.mean(errors[1]**2))),
        "maximum_raw_component_displacement_A": float(np.abs(after - before).max()),
    }


def verify_projection(before, after, policy):
    """Require the native setup operation itself, not just a looser distance cap."""
    if before.step != after.step or before.step != policy["step"]:
        raise ValidationError("SHAKE setup boundary step differs from bound evidence")
    if before.velocities is None or after.velocities is None or not np.array_equal(before.velocities, after.velocities):
        raise ValidationError("SHAKE setup boundary velocities changed or are unavailable")
    if before.cell_origin is None or after.cell_origin is None or not np.array_equal(before.cell, after.cell) or not np.array_equal(before.cell_origin, after.cell_origin):
        raise ValidationError("SHAKE setup boundary cell/origin changed or are unavailable")
    model = policy["model"]
    metrics = projection_metrics(before.positions, after.positions, before.cell, model["masses_Da"], model["pairs_zero_based"], model["distances_A"])
    limits = {
        "maximum_periodic_component_displacement_A": 1e-4,
        "maximum_unconstrained_atom_component_displacement_A": 1e-12,
        "maximum_cluster_COM_component_displacement_A": 1e-10,
        "maximum_component_difference_from_position_only_SHAKE_solution_A": 1e-9,
        "maximum_constraint_distance_error_before_A": 1e-4,
        "maximum_constraint_distance_error_after_A": 1e-8,
    }
    for metric, limit in limits.items():
        if metrics[metric] > limit:
            raise ValidationError(f"SHAKE setup projection rejected: {metric} > {limit}")
    return {"policy": policy["kind"], "evidence_sha256": policy["evidence_sha256"], "source_revision": policy["source_revision"], "metrics": metrics, "limits": limits,
            "cell_and_origin_exactly_unchanged": True, "velocities_exactly_unchanged": True,
            "previous_positions_float64_sha256": hashlib.sha256(before.positions.astype("<f8").tobytes()).hexdigest(),
            "next_positions_float64_sha256": hashlib.sha256(after.positions.astype("<f8").tobytes()).hexdigest(),
            "unchanged_velocities_float64_sha256": hashlib.sha256(before.velocities.astype("<f8").tobytes()).hexdigest(),
            "action": "retain preceding closed-step sample; separately preserve verified native SHAKE setup projection and both raw boundary records; no trajectory repair or bitwise continuation claim"}
