"""Persistent exact-upstream ProteinMPNN adapter."""

from __future__ import annotations

import copy
import math
import os
import random
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import torch

from common.server import ClientError

UPSTREAM = Path(os.environ.get("FS2_UPSTREAM_ROOT", "/opt/fs2/model/upstream"))
CHECKPOINT = UPSTREAM / "vanilla_model_weights/v_48_020.pt"
ALPHABET = "ACDEFGHIKLMNPQRSTVWY"


class Adapter:
    paths = {"/v1/infer", "/biology/ipd/proteinmpnn/predict"}
    native_response_paths = frozenset({"/biology/ipd/proteinmpnn/predict"})
    identity = {
        "candidate_id": "proteinmpnn-upstream-2023-06",
        "model_id": "dauparas/ProteinMPNN",
        "revision": "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57",
        "checkpoint": "vanilla_model_weights/v_48_020.pt",
        "checkpoint_sha256": "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd",
        "license": "MIT",
        "relationship": "same-named-upstream-contingency-nim-first",
        "nim_version": "1.2.0",
        "scope": "protein-sequence-design/research",
    }

    def __init__(self) -> None:
        self.device: torch.device | None = None
        self.model: Any | None = None
        self.utils: Any | None = None

    def load(self) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required")
        import protein_mpnn_utils as utils

        device = torch.device("cuda:0")
        checkpoint = torch.load(CHECKPOINT, map_location=device, weights_only=False)
        model = utils.ProteinMPNN(
            ca_only=False,
            num_letters=21,
            node_features=128,
            edge_features=128,
            hidden_dim=128,
            num_encoder_layers=3,
            num_decoder_layers=3,
            augment_eps=0.0,
            k_neighbors=checkpoint["num_edges"],
        )
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        model.to(device)
        model.eval()
        self.device = device
        self.model = model
        self.utils = utils

    def infer(self, request: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "input_pdb",
            "input_pdb_chains",
            "random_seed",
            "num_seq_per_target",
            "sampling_temp",
            "omit_AAs",
        }
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ClientError(f"unsupported fields: {', '.join(unknown)}")
        pdb = request.get("input_pdb")
        if not isinstance(pdb, str) or not (40 <= len(pdb.encode("utf-8")) <= 2_000_000):
            raise ClientError("input_pdb must contain 40..2000000 UTF-8 bytes")
        if "ATOM" not in pdb:
            raise ClientError("input_pdb contains no ATOM records")
        chains_value = request.get("input_pdb_chains")
        if chains_value is not None:
            if (
                not isinstance(chains_value, list)
                or not chains_value
                or len(chains_value) > 16
                or any(not isinstance(value, str) or len(value) != 1 for value in chains_value)
            ):
                raise ClientError("input_pdb_chains must be 1..16 one-character chain IDs")
            requested_chains = chains_value
        else:
            requested_chains = None
        seed = request.get("random_seed", 1)
        if not isinstance(seed, int) or isinstance(seed, bool) or not 1 <= seed <= 2**31 - 1:
            raise ClientError("random_seed must be an integer in [1, 2147483647]")
        count = request.get("num_seq_per_target", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 8:
            raise ClientError("num_seq_per_target must be an integer in [1, 8]")
        temperature_value = request.get("sampling_temp", [0.1])
        if isinstance(temperature_value, (int, float)) and not isinstance(temperature_value, bool):
            temperature = float(temperature_value)
        elif (
            isinstance(temperature_value, list)
            and len(temperature_value) == 1
            and isinstance(temperature_value[0], (int, float))
            and not isinstance(temperature_value[0], bool)
        ):
            temperature = float(temperature_value[0])
        else:
            raise ClientError("sampling_temp must be one finite number")
        if not math.isfinite(temperature) or not 0.01 <= temperature <= 1.0:
            raise ClientError("sampling_temp must be in [0.01, 1.0]")
        omit_value = request.get("omit_AAs", ["X"])
        if not isinstance(omit_value, list) or any(value not in ALPHABET + "X" for value in omit_value):
            raise ClientError("omit_AAs must contain one-letter amino-acid codes")

        assert self.device is not None and self.model is not None and self.utils is not None
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)
        random.seed(seed)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pdb", encoding="utf-8") as handle:
            handle.write(pdb)
            handle.flush()
            parsed = self.utils.parse_PDB(handle.name, ca_only=False)
        if not parsed:
            raise ClientError("input_pdb could not be parsed")
        dataset = self.utils.StructureDatasetPDB(parsed, truncate=None, max_length=20_000)
        if len(dataset) != 1:
            raise ClientError("input_pdb must resolve to one structure")
        protein = dataset[0]
        all_chains = sorted(key[-1:] for key in parsed[0] if key.startswith("seq_chain"))
        designed_chains = requested_chains or all_chains
        if not set(designed_chains).issubset(all_chains):
            raise ClientError("input_pdb_chains names a missing chain")
        fixed_chains = [value for value in all_chains if value not in designed_chains]
        chain_dict = {parsed[0]["name"]: (designed_chains, fixed_chains)}
        clones = [copy.deepcopy(protein) for _ in range(count)]

        with torch.inference_mode():
            values = self.utils.tied_featurize(
                clones,
                self.device,
                chain_dict,
                fixed_position_dict=None,
                omit_AA_dict=None,
                tied_positions_dict=None,
                pssm_dict=None,
                bias_by_res_dict=None,
                ca_only=False,
            )
            (
                x,
                native,
                mask,
                lengths,
                chain_mask,
                chain_encoding,
                _chain_lists,
                _visible_lists,
                masked_lists,
                masked_lengths,
                chain_position_mask,
                omit_mask,
                residue_idx,
                _dihedral_mask,
                _tied_positions,
                pssm_coef,
                pssm_bias,
                pssm_log_odds,
                bias_by_res,
                _tied_beta,
            ) = values
            omit = np.array([letter in set(omit_value) for letter in ALPHABET + "X"], dtype=np.float32)
            bias = np.zeros(21, dtype=np.float32)
            randn = torch.randn(chain_mask.shape, device=x.device)
            sampled = self.model.sample(
                x,
                randn,
                native,
                chain_mask,
                chain_encoding,
                residue_idx,
                mask=mask,
                temperature=temperature,
                omit_AAs_np=omit,
                bias_AAs_np=bias,
                chain_M_pos=chain_position_mask,
                omit_AA_mask=omit_mask,
                pssm_coef=pssm_coef,
                pssm_bias=pssm_bias,
                pssm_multi=0.0,
                pssm_log_odds_flag=False,
                pssm_log_odds_mask=(pssm_log_odds > 0).float(),
                pssm_bias_flag=False,
                bias_by_res=bias_by_res,
            )
            sequences = sampled["S"]
            log_probs = self.model(
                x,
                sequences,
                mask,
                chain_mask * chain_position_mask,
                residue_idx,
                chain_encoding,
                randn,
                use_input_decoding_order=True,
                decoding_order=sampled["decoding_order"],
            )
            design_mask = mask * chain_mask * chain_position_mask
            scores = self.utils._scores(sequences, log_probs, design_mask)
            global_scores = self.utils._scores(sequences, log_probs, mask)
            probabilities = sampled["probs"]
            recovery = torch.sum(
                (sequences == native).float() * design_mask, dim=1
            ) / torch.sum(design_mask, dim=1)

        outputs = []
        for index in range(count):
            sequence = self.utils._S_to_seq(sequences[index], chain_mask[index])
            chain_ids = masked_lists[index]
            chain_lengths = masked_lengths[index]
            cursor = 0
            chain_chunks = []
            for chain_id, chain_length in zip(chain_ids, chain_lengths, strict=True):
                chain_sequence = sequence[cursor : cursor + chain_length]
                cursor += chain_length
                chain_chunks.append(
                    {"chain_id": chain_id, "sequence": chain_sequence, "length": chain_length}
                )
            chains = sorted(chain_chunks, key=lambda item: item["chain_id"])
            if cursor != len(sequence) or any(set(item["sequence"]) - set(ALPHABET) for item in chains):
                raise RuntimeError("generated sequence failed the adapter semantic gate")
            outputs.append(
                {
                    "sample": index + 1,
                    "chains": chains,
                    "sequence": "/".join(item["sequence"] for item in chains),
                    "length": len(sequence),
                    "score": float(scores[index].item()),
                    "global_score": float(global_scores[index].item()),
                    "seq_recovery": float(recovery[index].item()),
                    "probabilities": probabilities[
                        index, : int(lengths[index]), :
                    ].detach().cpu().tolist(),
                }
            )
        return {
            "seed": seed,
            "temperature": temperature,
            "designed_chains": designed_chains,
            "native_sequence": str(protein["seq"]),
            "native_length": int(lengths[0]),
            "sequences": outputs,
        }

    def render_native_response(
        self, path: str, request: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        if path not in self.native_response_paths:
            raise RuntimeError("unsupported native response path")
        del request
        records = [
            f">input seed={output['seed']} designed_chains={output['designed_chains']!r}",
            output["native_sequence"],
        ]
        for sample in output["sequences"]:
            records.extend(
                [
                    f">sample={sample['sample']} score={sample['score']:.6f} "
                    f"global_score={sample['global_score']:.6f} "
                    f"seq_recovery={sample['seq_recovery']:.6f}",
                    sample["sequence"],
                ]
            )
        return {
            "mfasta": "\n".join(records) + "\n",
            "scores": [sample["score"] for sample in output["sequences"]],
            "probs": [sample["probabilities"] for sample in output["sequences"]],
        }
