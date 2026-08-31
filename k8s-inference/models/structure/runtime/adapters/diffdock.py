"""Persistent DiffDock v1.1 adapter with exact local ESM resolution."""

from __future__ import annotations

import copy
import math
import os
import random
import tempfile
from argparse import Namespace
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import torch
import yaml

from common.server import ClientError

UPSTREAM = Path(os.environ.get("FS2_UPSTREAM_ROOT", "/opt/fs2/model/upstream"))
MODEL_ROOT = Path(os.environ.get("FS2_MODEL_ROOT", "/opt/fs2/model/model"))
ESM_CHECKPOINT = MODEL_ROOT / "esm2_t33_650M_UR50D.pt"
ESM_REGRESSION = MODEL_ROOT / "esm2_t33_650M_UR50D-contact-regression.pt"


class Adapter:
    paths = {"/v1/infer", "/molecular-docking/diffdock/generate"}
    identity = {
        "candidate_id": "diffdock-upstream-v1-1",
        "model_id": "gcorso/DiffDock",
        "revision": "85c49b60d3e0b0182a59ee43a34a6d7036981284",
        "release": "v1.1",
        "release_archive_sha256": "5a95b6a1555be47ab1d6f0a8ffd25152f7fe32f5956005bb821e13e7a37d4a3d",
        "esm_checkpoint_sha256": "ea9d0522b335a8778dea6535a65301f10208dece28cd5865482b0b1fc446168c",
        "license": "MIT",
        "relationship": "same-named-upstream-fallback-parity-unproven",
        "nim_version": "2.3.0",
        "scope": "molecular-docking/research",
        "compatibility_shims": ["torch_cluster:pytorch-native", "torch_scatter:pytorch-native"],
    }

    def __init__(self) -> None:
        self.device: torch.device | None = None
        self.score_args: Namespace | None = None
        self.confidence_args: Namespace | None = None
        self.score_model: Any | None = None
        self.confidence_model: Any | None = None
        self.esm_model: Any | None = None
        self.esm_alphabet: Any | None = None
        self.schedule: Any | None = None
        self.t_to_sigma: Any | None = None

    def load(self) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required")
        from esm.pretrained import load_model_and_alphabet_core
        from utils.diffusion_utils import get_t_schedule, t_to_sigma as t_to_sigma_impl
        from utils.utils import get_model

        device = torch.device("cuda:0")
        score_args = Namespace(**yaml.safe_load((MODEL_ROOT / "score_model/model_parameters.yml").read_text()))
        # The upstream memory-saving crop can remove every receptor node when
        # the randomized ligand starts outside its cutoff, which produces a
        # non-retryable "No edges and no nodes" failure.  B300 memory is ample
        # for the bounded API, so retain the complete receptor graph.
        score_args.crop_beyond = None
        confidence_args = Namespace(
            **yaml.safe_load((MODEL_ROOT / "confidence_model/model_parameters.yml").read_text())
        )
        t_to_sigma = partial(t_to_sigma_impl, args=score_args)
        score_model = get_model(score_args, device, t_to_sigma=t_to_sigma, no_parallel=True, old=False)
        score_state = torch.load(
            MODEL_ROOT / "score_model/best_ema_inference_epoch_model.pt",
            map_location="cpu",
            weights_only=False,
        )
        score_model.load_state_dict(score_state, strict=True)
        score_model.to(device).eval()

        confidence_model = get_model(
            confidence_args,
            device,
            t_to_sigma=t_to_sigma,
            no_parallel=True,
            confidence_mode=True,
            old=True,
        )
        confidence_state = torch.load(
            MODEL_ROOT / "confidence_model/best_model_epoch75.pt",
            map_location="cpu",
            weights_only=False,
        )
        confidence_model.load_state_dict(confidence_state, strict=True)
        confidence_model.to(device).eval()

        esm_data = torch.load(ESM_CHECKPOINT, map_location="cpu", weights_only=False)
        regression_data = torch.load(ESM_REGRESSION, map_location="cpu", weights_only=False)
        esm_model, esm_alphabet = load_model_and_alphabet_core(
            "esm2_t33_650M_UR50D",
            esm_data,
            regression_data,
        )
        esm_model.to(device).eval()

        self.device = device
        self.score_args = score_args
        self.confidence_args = confidence_args
        self.score_model = score_model
        self.confidence_model = confidence_model
        self.esm_model = esm_model
        self.esm_alphabet = esm_alphabet
        self.schedule = get_t_schedule(inference_steps=20, sigma_schedule="expbeta")
        self.t_to_sigma = t_to_sigma

    def infer(self, request: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "protein",
            "ligand",
            "ligand_file_type",
            "num_poses",
            "time_divisions",
            "steps",
            "random_seed",
            "save_trajectory",
            "skip_gen_conformer",
        }
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ClientError(f"unsupported fields: {', '.join(unknown)}")
        protein = request.get("protein")
        ligand = request.get("ligand")
        if not isinstance(protein, str) or not (40 <= len(protein.encode()) <= 2_000_000):
            raise ClientError("protein must contain 40..2000000 UTF-8 PDB bytes")
        if "ATOM" not in protein:
            raise ClientError("protein contains no ATOM records")
        if not isinstance(ligand, str) or not 1 <= len(ligand) <= 4096:
            raise ClientError("ligand must be a 1..4096 character SMILES string")
        if request.get("ligand_file_type", "txt") != "txt":
            raise ClientError("only inline SMILES ligand_file_type=txt is supported")
        poses = request.get("num_poses", 1)
        divisions = request.get("time_divisions", 20)
        steps = request.get("steps", 18)
        seed = request.get("random_seed", 1)
        for name, value, minimum, maximum in (
            ("num_poses", poses, 1, 4),
            ("time_divisions", divisions, 3, 20),
            ("steps", steps, 1, 20),
            ("random_seed", seed, 1, 2**31 - 1),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ClientError(f"{name} must be an integer in [{minimum}, {maximum}]")
        if steps > divisions:
            raise ClientError("steps cannot exceed time_divisions")
        if request.get("save_trajectory", False) not in (False, None):
            raise ClientError("save_trajectory is not exposed by the bounded API")
        if request.get("skip_gen_conformer", False) not in (False, None):
            raise ClientError("skip_gen_conformer is not supported")

        assert all(
            value is not None
            for value in (
                self.device,
                self.score_args,
                self.confidence_args,
                self.score_model,
                self.confidence_model,
                self.esm_model,
                self.esm_alphabet,
                self.t_to_sigma,
            )
        )
        from datasets.process_mols import write_mol_with_coords
        from utils.diffusion_utils import get_t_schedule
        from utils.inference_utils import (
            InferenceDataset,
            compute_ESM_embeddings,
            get_sequences,
        )
        from utils.sampling import randomize_position, sampling
        from rdkit.Chem import RemoveAllHs

        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)
        random.seed(seed)
        schedule = get_t_schedule(inference_steps=divisions, sigma_schedule="expbeta")

        with tempfile.TemporaryDirectory(prefix="fs2-diffdock-") as directory:
            root = Path(directory)
            protein_path = root / "receptor.pdb"
            protein_path.write_text(protein, encoding="utf-8")
            sequences = get_sequences([str(protein_path)], [None])[0].split(":")
            labels = [f"request_chain_{index}" for index in range(len(sequences))]
            with torch.inference_mode():
                embeddings = compute_ESM_embeddings(
                    self.esm_model,
                    self.esm_alphabet,
                    labels,
                    sequences,
                )
            precomputed = [[embeddings[label] for label in labels]]
            dataset = InferenceDataset(
                out_dir=str(root),
                complex_names=["request"],
                protein_files=[str(protein_path)],
                ligand_descriptions=[ligand],
                protein_sequences=[None],
                lm_embeddings=True,
                receptor_radius=self.score_args.receptor_radius,
                c_alpha_max_neighbors=self.score_args.c_alpha_max_neighbors,
                precomputed_lm_embeddings=precomputed,
                remove_hs=self.score_args.remove_hs,
                all_atoms=self.score_args.all_atoms,
                atom_radius=self.score_args.atom_radius,
                atom_max_neighbors=self.score_args.atom_max_neighbors,
                knn_only_graph=not getattr(self.score_args, "not_knn_only_graph", True),
            )
            original = dataset[0]
            if not bool(original.success):
                raise ClientError("protein-ligand graph construction failed")
            confidence_dataset = InferenceDataset(
                out_dir=str(root),
                complex_names=["request"],
                protein_files=[str(protein_path)],
                ligand_descriptions=[ligand],
                protein_sequences=[None],
                lm_embeddings=True,
                receptor_radius=self.confidence_args.receptor_radius,
                c_alpha_max_neighbors=self.confidence_args.c_alpha_max_neighbors,
                precomputed_lm_embeddings=precomputed,
                remove_hs=self.confidence_args.remove_hs,
                all_atoms=self.confidence_args.all_atoms,
                atom_radius=self.confidence_args.atom_radius,
                atom_max_neighbors=self.confidence_args.atom_max_neighbors,
                knn_only_graph=False,
            )
            confidence_original = confidence_dataset[0]
            if not bool(confidence_original.success):
                raise ClientError("confidence graph construction failed")
            data_list = [copy.deepcopy(original) for _ in range(poses)]
            confidence_list = [copy.deepcopy(confidence_original) for _ in range(poses)]
            # DiffDock's normal DataLoader path wraps this per-graph torsion mask
            # in a one-element list.  We invoke sampling directly, so preserve
            # that shape contract before randomize_position indexes element 0.
            for graph in data_list:
                mask_rotate = graph["ligand"].mask_rotate
                if isinstance(mask_rotate, np.ndarray):
                    graph["ligand"].mask_rotate = [mask_rotate]
            randomize_position(
                data_list,
                self.score_args.no_torsion,
                False,
                self.score_args.tr_sigma_max,
                initial_noise_std_proportion=1.4601642460337794,
                choose_residue=False,
            )
            with torch.inference_mode():
                data_list, confidence = sampling(
                    data_list=data_list,
                    model=self.score_model,
                    inference_steps=steps,
                    tr_schedule=schedule,
                    rot_schedule=schedule,
                    tor_schedule=schedule,
                    device=self.device,
                    t_to_sigma=self.t_to_sigma,
                    model_args=self.score_args,
                    confidence_model=self.confidence_model,
                    confidence_data_list=confidence_list,
                    confidence_model_args=self.confidence_args,
                    batch_size=poses,
                    no_final_step_noise=True,
                    temp_sampling=[1.170050527854316, 2.06391612594481, 7.044261621607846],
                    temp_psi=[0.727287304570729, 0.9022615585667628, 0.5946212391366862],
                    temp_sigma_data=[0.9299802531572672, 0.7464326999906034, 0.6943254174849822],
                )
            if confidence is None:
                raise RuntimeError("confidence model returned no values")
            confidence_values = confidence[:, 0] if confidence.ndim > 1 else confidence
            order = torch.argsort(confidence_values, descending=True).tolist()
            ligand_template = original.mol[0] if isinstance(original.mol, (list, tuple)) else original.mol
            pose_outputs = []
            for rank, data_index in enumerate(order, 1):
                coordinates = (
                    data_list[data_index]["ligand"].pos.detach().cpu().numpy()
                    + original.original_center.detach().cpu().numpy()
                )
                molecule = copy.deepcopy(ligand_template)
                if self.score_args.remove_hs:
                    molecule = RemoveAllHs(molecule)
                sdf_path = root / f"rank{rank}.sdf"
                write_mol_with_coords(molecule, coordinates, str(sdf_path))
                sdf = sdf_path.read_text(encoding="utf-8")
                value = float(confidence_values[data_index].item())
                if not math.isfinite(value) or "V2000" not in sdf or "M  END" not in sdf:
                    raise RuntimeError("generated pose failed the adapter semantic gate")
                pose_outputs.append({"rank": rank, "confidence": value, "sdf": sdf})
        return {
            "seed": seed,
            "ligand": ligand,
            "protein_bytes": len(protein.encode()),
            "poses": pose_outputs,
        }
