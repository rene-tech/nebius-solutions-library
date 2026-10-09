"""Bounded valid-yield collection, independent of CUDA/runtime initialization."""

from __future__ import annotations

import math
from typing import Any

from rdkit import Chem
from rdkit.Chem import Crippen, QED


class GenerationExhausted(RuntimeError):
    """Sampling finished without the requested valid/unique molecule count."""

    def __init__(self, metrics: dict[str, Any]) -> None:
        super().__init__("GenMol exhausted bounded sampling before meeting the requested molecule count")
        self.metrics = metrics


def generate_valid_molecules(
    sampler: Any,
    *,
    requested: int,
    temperature: float,
    randomness: float,
    token_length: int,
    scoring: str,
    unique: bool,
    max_sampling_attempts: int = 8,
    candidate_multiplier: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Refill only missing candidates; never repeat/fabricate molecules to pad.

    The caller's existing midpoint minimum-mask length, temperature, noise and
    property calculation remain unchanged. The token length is not a bound on
    heavy atoms. Replenishment is rejection sampling, not an optimizer.
    """
    if requested < 1 or max_sampling_attempts < 1 or candidate_multiplier < 1:
        raise ValueError("Generation count and sampling budgets must be positive")
    if scoring not in {"QED", "LOGP"}:
        raise ValueError("Unsupported molecular scoring method")
    score_function = QED.qed if scoring == "QED" else Crippen.MolLogP
    molecules: list[dict[str, Any]] = []
    seen: set[str] = set()
    metrics = {
        "requested_molecules": requested,
        "returned_molecules": 0,
        "accepted_molecules": 0,
        "sampling_attempts": 0,
        "candidate_requests": 0,
        "sampled_candidates": 0,
        "upstream_unreturned_candidates": 0,
        "invalid_candidates": 0,
        "duplicate_candidates": 0,
        "nonfinite_score_candidates": 0,
        "unique_requested": unique,
        "minimum_mask_tokens": token_length,
        "max_sampling_attempts": max_sampling_attempts,
        "max_candidate_requests": requested * candidate_multiplier,
    }
    while len(molecules) < requested and metrics["sampling_attempts"] < max_sampling_attempts:
        remaining_budget = metrics["max_candidate_requests"] - metrics["candidate_requests"]
        draw_count = min(requested - len(molecules), remaining_budget)
        if draw_count <= 0:
            break
        metrics["sampling_attempts"] += 1
        metrics["candidate_requests"] += draw_count
        samples = sampler.de_novo_generation(draw_count, temperature, randomness, token_length)
        if not isinstance(samples, (list, tuple)) or len(samples) > draw_count:
            raise RuntimeError("GenMol sampler returned an unexpected candidate collection")
        metrics["sampled_candidates"] += len(samples)
        metrics["upstream_unreturned_candidates"] += draw_count - len(samples)
        for smiles in samples:
            molecule = Chem.MolFromSmiles(smiles) if isinstance(smiles, str) else None
            if molecule is None or molecule.GetNumAtoms() < 1:
                metrics["invalid_candidates"] += 1
                continue
            canonical = Chem.MolToSmiles(molecule)
            if unique and canonical in seen:
                metrics["duplicate_candidates"] += 1
                continue
            score = float(score_function(molecule))
            if not math.isfinite(score):
                metrics["nonfinite_score_candidates"] += 1
                continue
            seen.add(canonical)
            molecules.append({"smiles": canonical, "score": score})
    metrics["accepted_molecules"] = len(molecules)
    if len(molecules) != requested:
        # An error response returns no successful result. Keep accepted partial
        # yield explicit rather than calling it a full or empty success.
        raise GenerationExhausted(metrics)
    metrics["returned_molecules"] = len(molecules)
    return molecules, metrics
