"""Auditable geometry in Angstrom; all transforms operate on copies."""
from itertools import product

import numpy as np


class ValidationError(ValueError):
    """Scientific input does not satisfy the declared comparison contract."""


def finite(value, label):
    a = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(a)):
        raise ValidationError(f"non-finite {label}")
    return a


def validate_cell(cell):
    cell = finite(cell, "periodic cell")
    if cell.shape != (3, 3) or np.linalg.det(cell) <= 1e-8:
        raise ValidationError("periodic cell must be a right-handed nonsingular 3x3 matrix")
    # The 27-image nearest-neighbor search below assumes a reduced cell. Reject
    # pathological lattices explicitly instead of silently producing wrong PBC.
    lengths = np.linalg.norm(cell, axis=1)
    cosines = (cell @ cell.T) / np.outer(lengths, lengths)
    if np.any(np.abs(cosines[np.triu_indices(3, 1)]) > 0.5 + 1e-6):
        raise ValidationError("cell is not reduced (angles must lie within 60..120 degrees)")
    return cell


def minimum_image(displacement, cell):
    cell = validate_cell(cell)
    delta = finite(displacement, "displacements")
    shape = delta.shape
    if shape[-1] != 3:
        raise ValidationError("displacements must end in xyz")
    flat = delta.reshape(-1, 3)
    frac = flat @ np.linalg.inv(cell)
    base = (frac - np.rint(frac)) @ cell
    if np.allclose(cell, np.diag(np.diag(cell)), atol=1e-10):
        return base.reshape(shape)
    best = base.copy()
    best2 = np.einsum("ij,ij->i", best, best)
    for offset in product((-1, 0, 1), repeat=3):
        candidate = base + np.asarray(offset) @ cell
        norm2 = np.einsum("ij,ij->i", candidate, candidate)
        mask = norm2 < best2
        best[mask], best2[mask] = candidate[mask], norm2[mask]
    return best.reshape(shape)


def make_whole(positions, bonds, cell):
    """Reconstruct one connected molecule by bonded minimum-image traversal."""
    positions = finite(positions, "molecule coordinates")
    if positions.ndim != 2 or positions.shape[1] != 3 or not len(positions):
        raise ValidationError("empty or malformed molecule")
    graph = [[] for _ in positions]
    for a, b in np.asarray(bonds, dtype=int):
        if a == b or min(a, b) < 0 or max(a, b) >= len(positions):
            raise ValidationError("invalid molecule bond")
        graph[a].append(b)
        graph[b].append(a)
    out = positions.copy()
    visited, queue = {0}, [0]
    while queue:
        a = queue.pop()
        for b in graph[a]:
            candidate = out[a] + minimum_image(positions[b] - positions[a], cell)
            if b in visited:
                if np.linalg.norm(candidate - out[b]) > 1e-4:
                    raise ValidationError("inconsistent periodic molecular bond cycle")
            else:
                out[b] = candidate
                visited.add(b)
                queue.append(b)
    if len(visited) != len(positions):
        raise ValidationError("peptide bond graph is disconnected")
    return out


def kabsch(mobile, reference):
    """Proper rotation for row vectors, after callers center both structures."""
    mobile, reference = finite(mobile, "alignment"), finite(reference, "reference")
    if mobile.shape != reference.shape or mobile.ndim != 2 or mobile.shape[1] != 3:
        raise ValidationError("incompatible alignment coordinates")
    if np.linalg.matrix_rank(mobile) < 2 or np.linalg.matrix_rank(reference) < 2:
        raise ValidationError("alignment needs at least three non-collinear points")
    u, _, vt = np.linalg.svd(mobile.T @ reference)
    correction = np.eye(3)
    correction[-1, -1] = np.sign(np.linalg.det(u @ vt))
    rotation = u @ correction @ vt
    if not np.isclose(np.linalg.det(rotation), 1):
        raise ValidationError("alignment introduced a reflection")
    return rotation


def dihedral(points):
    p = finite(points, "dihedral")
    if p.shape != (4, 3):
        raise ValidationError("dihedral requires four atoms")
    b0, b1, b2 = -(p[1] - p[0]), p[2] - p[1], p[3] - p[2]
    norm = np.linalg.norm(b1)
    if norm < 1e-10:
        raise ValidationError("degenerate dihedral central bond")
    b1 /= norm
    v, w = b0 - np.dot(b0, b1) * b1, b2 - np.dot(b2, b1) * b1
    if min(np.linalg.norm(v), np.linalg.norm(w)) < 1e-10:
        raise ValidationError("collinear dihedral")
    return float(np.degrees(np.arctan2(np.dot(np.cross(b1, v), w), np.dot(v, w))))


def align_frame(positions, cell, peptide_indices, peptide_bonds, fit_indices, reference, waters):
    """Whole peptide -> fit-center -> common reference; solvent nearest images.

    waters contains only oxygen indices for a transparent point representation.
    The raw positions/trajectory are never modified or replaced.
    """
    positions = finite(positions, "all trajectory coordinates")
    peptide = make_whole(positions[peptide_indices], peptide_bonds, cell)
    center = peptide[fit_indices].mean(axis=0)
    peptide -= center
    rotation = kabsch(peptide[fit_indices], reference[fit_indices])
    solvent = minimum_image(positions[waters] - center, cell) @ rotation
    return peptide @ rotation, solvent
