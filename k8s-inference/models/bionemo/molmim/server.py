"""GPU-portable PyTorch runtime for the exact retained MolMIM 70M weights.

The retained NIM profile is tied to an obsolete software stack. This runtime
reads the same `.nemo` state dictionary and implements its legacy Megatron
attention graph with stock PyTorch CUDA operations. Numerical parity with the
NIM is not claimed; model identity and checkpoint bytes are retained.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import re
import tarfile
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import torch
import torch.nn.functional as F
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import AllChem, QED

# The service does not use pycma's optional plotting integration.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="Could not import matplotlib.pyplot.*", module="cma.s"
    )
    import cma


LOGGER = logging.getLogger("fs2.molmim")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SOURCE_REVISION = os.environ["BIONEMO_SOURCE_REVISION"]
NEMO_PATH = Path(os.environ["MOLMIM_NEMO"])
NEMO_SHA256 = os.environ["MOLMIM_NEMO_SHA256"]
WEIGHTS_SHA256 = os.environ["MOLMIM_WEIGHTS_SHA256"]
HIDDEN = 512
HEADS = 8
HEAD_DIM = HIDDEN // HEADS
MAX_TOKENS = 128
PORT_REVISION = "perceiver-cmaes-20260918"


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smi: str = Field(min_length=1, max_length=512)
    algorithm: Literal["CMA-ES"] = "CMA-ES"
    num_molecules: int = Field(default=1, ge=1, le=16)
    property_name: Literal["QED"] = "QED"
    minimize: bool = False
    min_similarity: float = Field(default=0.3, ge=0, le=1)
    particles: int = Field(default=2, ge=2, le=32)
    iterations: int = Field(default=1, ge=1, le=16)
    radius: float = Field(default=1.0, gt=0, le=10)

    @model_validator(mode="after")
    def enough_candidates(self) -> GenerateRequest:
        if self.num_molecules > self.particles * self.iterations:
            raise ValueError(
                "num_molecules exceeds particles * iterations decode budget"
            )
        return self


class GenerationExhausted(Exception):
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        super().__init__(
            "Optimization did not produce the requested number of distinct feasible molecules"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RegexTokenizer:
    def __init__(self, pattern: str, vocabulary: str) -> None:
        # NeMo RegExTokenizer appends a single-character fallback. In this
        # checkpoint it is needed for the single backslash stereobond token.
        self.pattern = re.compile("(" + pattern.strip() + "|.)")
        self.tokens = vocabulary.splitlines()
        self.token_to_id = {token: index for index, token in enumerate(self.tokens)}
        self.pad_id = self.token_to_id["<PAD>"]
        self.unk_id = self.token_to_id["?"]
        self.bos_id = self.token_to_id["^"]
        self.eos_id = self.token_to_id["&"]
        forbidden_prefixes = ("<", "LogD_change_", "Solubility_", "Clint_")
        self.sample_ids = [
            index
            for index, token in enumerate(self.tokens)
            if not token.startswith(forbidden_prefixes) and token not in {"?", "^", "&"}
        ] + [self.eos_id]

    def encode(self, value: str) -> list[int]:
        tokens = self.pattern.findall(value)
        if "".join(tokens) != value:
            raise ValueError("SMILES contains a token outside the MolMIM vocabulary")
        if any(token not in self.token_to_id for token in tokens):
            raise ValueError("SMILES contains a token outside the MolMIM vocabulary")
        return [self.token_to_id[token] for token in tokens]

    def decode(self, ids: list[int]) -> str:
        output: list[str] = []
        for token_id in ids:
            if token_id == self.eos_id:
                break
            if 0 <= token_id < len(self.tokens):
                token = self.tokens[token_id]
                if token not in {"<PAD>", "?", "^", "<MASK>", "<SEP>"}:
                    output.append(token)
        return "".join(output)


class MolMIMPort:
    def __init__(
        self,
        state: dict[str, torch.Tensor],
        tokenizer: RegexTokenizer,
        *,
        device: str = "cuda:0",
    ) -> None:
        self.device = torch.device(device)
        self.state = {key: value.to(device=self.device) for key, value in state.items()}
        self.tokenizer = tokenizer
        allowed = torch.zeros(640, device=self.device, dtype=torch.bool)
        allowed[tokenizer.sample_ids] = True
        self.allowed_tokens = allowed

    def _p(self, name: str) -> torch.Tensor:
        return self.state[name]

    def _linear(self, value: torch.Tensor, prefix: str) -> torch.Tensor:
        return F.linear(value, self._p(prefix + ".weight"), self._p(prefix + ".bias"))

    def _layer_norm(self, value: torch.Tensor, prefix: str) -> torch.Tensor:
        return F.layer_norm(
            value,
            (HIDDEN,),
            self._p(prefix + ".weight"),
            self._p(prefix + ".bias"),
            1e-5,
        )

    def _attention(
        self,
        value: torch.Tensor,
        prefix: str,
        *,
        memory: torch.Tensor | None = None,
        causal: bool = False,
    ) -> torch.Tensor:
        batch, length, _ = value.shape
        if memory is None:
            mixed = self._linear(value, prefix + ".query_key_value")
            mixed = mixed.view(batch, length, HEADS, 3, HEAD_DIM)
            query, key, values = mixed.unbind(dim=3)
        else:
            query = self._linear(value, prefix + ".query").view(
                batch, length, HEADS, HEAD_DIM
            )
            memory_length = memory.shape[1]
            mixed = self._linear(memory, prefix + ".key_value").view(
                batch, memory_length, HEADS, 2, HEAD_DIM
            )
            key, values = mixed.unbind(dim=3)
        query = query.permute(0, 2, 1, 3)
        key = key.permute(0, 2, 1, 3)
        values = values.permute(0, 2, 1, 3)
        scores = torch.matmul(query, key.transpose(-1, -2)) / math.sqrt(HEAD_DIM)
        if causal:
            mask = torch.ones(
                (length, key.shape[-2]), device=value.device, dtype=torch.bool
            ).triu(diagonal=1)
            scores = scores.masked_fill(mask, torch.finfo(scores.dtype).min)
        probabilities = torch.softmax(scores, dim=-1)
        context = torch.matmul(probabilities, values)
        context = context.permute(0, 2, 1, 3).contiguous().view(batch, length, HIDDEN)
        return self._linear(context, prefix + ".dense")

    def _layer(
        self,
        value: torch.Tensor,
        prefix: str,
        *,
        memory: torch.Tensor | None,
        causal: bool,
    ) -> torch.Tensor:
        normalized = self._layer_norm(value, prefix + ".input_layernorm")
        value = value + self._attention(
            normalized, prefix + ".self_attention", causal=causal
        )
        normalized = self._layer_norm(value, prefix + ".post_attention_layernorm")
        if memory is not None:
            value = value + self._attention(
                normalized, prefix + ".inter_attention", memory=memory
            )
            normalized = self._layer_norm(
                value, prefix + ".post_inter_attention_layernorm"
            )
        hidden = self._linear(normalized, prefix + ".mlp.dense_h_to_4h")
        # The checkpoint uses NeMo bias_activation_fusion, whose GELU is tanh.
        hidden = F.gelu(hidden, approximate="tanh")
        value = value + self._linear(hidden, prefix + ".mlp.dense_4h_to_h")
        return value

    def encode(self, smiles: str) -> torch.Tensor:
        ids = self.tokenizer.encode(smiles)
        if not ids or len(ids) > MAX_TOKENS:
            raise ValueError("SMILES token length is outside 1..128")
        tokens = torch.tensor([ids], device=self.device, dtype=torch.long)
        positions = torch.arange(len(ids), device=self.device).unsqueeze(0)
        base = "enc_dec_model"
        embedded = F.embedding(
            tokens, self._p(base + ".encoder_embedding.word_embeddings.weight")
        ) + F.embedding(
            positions, self._p(base + ".encoder_embedding.position_embeddings.weight")
        )
        hidden = self._p(base + ".enc_dec_model.encoder.init_hidden").unsqueeze(0)
        for index in range(6):
            residual = hidden
            prefix = base + f".enc_dec_model.encoder.cross_attn_layers.{index}.layers.0"
            hidden = self._layer(hidden, prefix, memory=embedded, causal=False)
            prefix = base + f".enc_dec_model.encoder.self_attn_layers.{index}.layers.0"
            hidden = self._layer(hidden, prefix, memory=None, causal=False)
            # NeMo MegatronPerceiverEncoderModule has an OUTER residual in
            # addition to the residuals inside each cross/self-attention layer.
            hidden = hidden + residual
        hidden = self._layer_norm(
            hidden, base + ".enc_dec_model.encoder.final_layernorm"
        )
        return self._linear(
            hidden,
            base + ".enc_dec_model.hiddens_module.hidden_transforms.0.hiddens_to_mean",
        )

    def logits(self, tokens: torch.Tensor, latent: torch.Tensor) -> torch.Tensor:
        positions = torch.arange(tokens.shape[1], device=self.device).unsqueeze(0)
        base = "enc_dec_model"
        hidden = F.embedding(
            tokens, self._p(base + ".decoder_embedding.word_embeddings.weight")
        ) + F.embedding(
            positions, self._p(base + ".decoder_embedding.position_embeddings.weight")
        )
        for index in range(6):
            prefix = base + f".enc_dec_model.decoder.model.layers.{index}"
            hidden = self._layer(hidden, prefix, memory=latent, causal=True)
        hidden = self._layer_norm(
            hidden, base + ".enc_dec_model.decoder.model.final_layernorm"
        )
        return self._linear(hidden[:, -1], base + ".tokens_head")

    def decode(self, latent: torch.Tensor) -> list[str]:
        """Greedy (beam-size one) decoding, as in upstream controlled generation.

        The complete checkpoint positional window is available. An unfinished
        sequence is invalid, never silently accepted as a truncated molecule.
        """
        tokens = torch.full(
            (latent.shape[0], 1),
            self.tokenizer.bos_id,
            device=self.device,
            dtype=torch.long,
        )
        finished = torch.zeros(latent.shape[0], device=self.device, dtype=torch.bool)
        for _ in range(MAX_TOKENS):
            logits = self.logits(tokens, latent).masked_fill(
                ~self.allowed_tokens, float("-inf")
            )
            if not torch.isfinite(logits[:, self.allowed_tokens]).all():
                raise RuntimeError("MolMIM decoder produced nonfinite logits")
            selected = logits.argmax(dim=-1)
            selected = torch.where(finished, self.tokenizer.eos_id, selected)
            tokens = torch.cat((tokens, selected.unsqueeze(1)), dim=1)
            finished = finished | (selected == self.tokenizer.eos_id)
            if finished.all():
                break
        return [
            self.tokenizer.decode(ids[1:]) if done else ""
            for ids, done in zip(tokens.tolist(), finished.tolist(), strict=True)
        ]


class Runtime:
    model: MolMIMPort | None = None
    ready = False
    startup_seconds = 0.0
    requests = 0
    failures = 0
    generated = 0
    compute_capability = "unknown"
    lock = asyncio.Lock()


RUNTIME = Runtime()


def _load_runtime() -> None:
    started = time.monotonic()
    if _sha256(NEMO_PATH) != NEMO_SHA256:
        raise RuntimeError("MolMIM .nemo digest mismatch")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    capability = torch.cuda.get_device_capability(0)
    RUNTIME.compute_capability = f"{capability[0]}.{capability[1]}"
    with tarfile.open(NEMO_PATH, mode="r") as archive:
        weights_member = archive.getmember("./model_weights.ckpt")
        weights_source = archive.extractfile(weights_member)
        model_member = archive.getmember(
            "./048c1f797f464dd5b6a90f60f9405827_molmim.model"
        )
        vocab_member = archive.getmember(
            "./dd344353154640acbbaea1d4536fa7d0_molmim.vocab"
        )
        model_source = archive.extractfile(model_member)
        vocab_source = archive.extractfile(vocab_member)
        if weights_source is None or model_source is None or vocab_source is None:
            raise RuntimeError("MolMIM .nemo archive is incomplete")
        weights_bytes = weights_source.read()
        if hashlib.sha256(weights_bytes).hexdigest() != WEIGHTS_SHA256:
            raise RuntimeError("MolMIM state dictionary digest mismatch")
        state = torch.load(
            io.BytesIO(weights_bytes), map_location="cpu", weights_only=True
        )
        tokenizer = RegexTokenizer(
            model_source.read().decode("utf-8"),
            vocab_source.read().decode("utf-8"),
        )
    RUNTIME.model = MolMIMPort(state, tokenizer)
    # Execute an actual encoder pass before declaring readiness.
    with torch.inference_mode():
        warmup = RUNTIME.model.encode("CC(=O)O")
        if warmup.shape != (1, 1, HIDDEN) or not torch.isfinite(warmup).all():
            raise RuntimeError("MolMIM model warmup failed")
        reconstruction = RUNTIME.model.decode(
            RUNTIME.model.encode("CC(=O)Oc1ccccc1C(=O)O")
        )
        if reconstruction != ["CC(=O)Oc1ccccc1C(=O)O"]:
            raise RuntimeError("MolMIM decoder reconstruction warmup failed")
    RUNTIME.startup_seconds = time.monotonic() - started
    RUNTIME.ready = True
    LOGGER.info(
        "molmim runtime ready source=%s nemo_sha256=%s weights_sha256=%s "
        "gpu=%s startup_seconds=%.3f",
        SOURCE_REVISION,
        NEMO_SHA256,
        WEIGHTS_SHA256,
        torch.cuda.get_device_name(0),
        RUNTIME.startup_seconds,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(_load_runtime)
    yield


app = FastAPI(
    title="FS2 MolMIM portable CUDA port",
    version=SOURCE_REVISION[:12],
    lifespan=lifespan,
)


def _similarity(first: Chem.Mol, second: Chem.Mol) -> float:
    return float(
        DataStructs.TanimotoSimilarity(
            AllChem.GetMorganGenerator(radius=2).GetFingerprint(first),
            AllChem.GetMorganGenerator(radius=2).GetFingerprint(second),
        )
    )


def _generate(request: GenerateRequest) -> dict[str, Any]:
    if RUNTIME.model is None:
        raise RuntimeError("model is not loaded")
    source = Chem.MolFromSmiles(request.smi)
    if source is None:
        raise ValueError("smi is not a valid molecule")
    canonical_source = Chem.MolToSmiles(source)
    candidates: dict[str, tuple[float, float]] = {}
    counts = {
        "attempted_model_decodes": 0,
        "invalid_decodes": 0,
        "below_similarity": 0,
        "unchanged_decodes": 0,
        "duplicate_decodes": 0,
        "optimizer_steps": 0,
    }
    base_seed = int.from_bytes(
        hashlib.sha256(canonical_source.encode()).digest()[:4], "big"
    )
    with torch.inference_mode():
        encoded = RUNTIME.model.encode(canonical_source)
        optimizer = cma.CMAEvolutionStrategy(
            encoded.cpu().numpy().reshape(-1).astype(np.float64),
            0.75 * request.radius,
            {
                "popsize": request.particles,
                "randn": np.random.RandomState(base_seed).randn,
                "seed": np.nan,
                "verbose": -9,
            },
        )
        for _ in range(request.iterations):
            population = optimizer.ask()
            latents = torch.as_tensor(
                np.asarray(population), device=encoded.device, dtype=encoded.dtype
            ).reshape(-1, 1, HIDDEN)
            decoded = RUNTIME.model.decode(latents)
            if len(decoded) != len(population):
                raise RuntimeError("MolMIM decoder population size mismatch")
            objectives: list[float] = []
            for sampled in decoded:
                counts["attempted_model_decodes"] += 1
                # Invalid exploratory decodes are counted below; suppress only
                # RDKit's per-candidate parser chatter, not service exceptions.
                with rdBase.BlockLogs():
                    molecule = Chem.MolFromSmiles(sampled) if sampled else None
                if molecule is None:
                    counts["invalid_decodes"] += 1
                    objectives.append(10.0)
                    continue
                canonical = Chem.MolToSmiles(molecule)
                similarity = _similarity(source, molecule)
                score = float(QED.qed(molecule))
                # Upstream BioNeMo controlled-generation objective: maximize
                # clipped relative similarity plus directed QED / 0.9. pycma
                # minimizes; final outputs ALSO enforce the advertised bound.
                relative_similarity = (
                    min(similarity / request.min_similarity, 1.0)
                    if request.min_similarity
                    else 1.0
                )
                directed_score = -score if request.minimize else score
                objectives.append(-(relative_similarity + directed_score / 0.9))
                if similarity < request.min_similarity:
                    counts["below_similarity"] += 1
                elif canonical == canonical_source:
                    counts["unchanged_decodes"] += 1
                elif canonical in candidates:
                    counts["duplicate_decodes"] += 1
                else:
                    candidates[canonical] = (score, similarity)
            optimizer.tell(population, objectives)
            counts["optimizer_steps"] += 1
    counts["distinct_feasible_molecules"] = len(candidates)
    counts["requested_molecules"] = request.num_molecules
    if len(candidates) < request.num_molecules:
        raise GenerationExhausted(counts)
    chosen = sorted(
        candidates.items(), key=lambda item: item[1][0], reverse=not request.minimize
    )[: request.num_molecules]
    RUNTIME.generated += len(chosen)
    return {
        "generated": [
            {
                "sample": smiles,
                "score": score,
                "similarity": similarity,
                "model_decoded": True,
                "changed_from_input": True,
            }
            for smiles, (score, similarity) in chosen
        ],
        "metrics": {
            "algorithm": "CMA-ES",
            "optimizer": "pycma-4.4.0",
            "port_revision": PORT_REVISION,
            "initial_sigma": 0.75 * request.radius,
            "decoder": "greedy-full-window",
            **counts,
            "source_qed": float(QED.qed(source)),
            "checkpoint_sha256": NEMO_SHA256,
            "runtime_relationship": "exact-weights-independent-blackwell-port",
        },
    }


@app.get("/v1/health/ready")
def ready() -> dict[str, Any]:
    if not RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model is loading")
    return {
        "status": "ready",
        "model": "molmim",
        "source_revision": SOURCE_REVISION,
        "port_revision": PORT_REVISION,
        "nemo_sha256": NEMO_SHA256,
        "weights_sha256": WEIGHTS_SHA256,
        "runtime_relationship": "exact-weights-independent-blackwell-port",
        "nim_numerical_parity": "unverified",
        "compute_capability": RUNTIME.compute_capability,
        "startup_seconds": round(RUNTIME.startup_seconds, 6),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "\n".join(
        (
            "# TYPE fs2_model_requests_total counter",
            f'fs2_model_requests_total{{model="molmim"}} {RUNTIME.requests}',
            "# TYPE fs2_model_failures_total counter",
            f'fs2_model_failures_total{{model="molmim"}} {RUNTIME.failures}',
            "# TYPE fs2_model_outputs_total counter",
            f'fs2_model_outputs_total{{model="molmim"}} {RUNTIME.generated}',
            "",
        )
    )


@app.post("/generate")
async def generate(request: GenerateRequest) -> dict[str, Any]:
    if not RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model is loading")
    RUNTIME.requests += 1
    try:
        async with RUNTIME.lock:
            return await asyncio.to_thread(_generate, request)
    except GenerationExhausted as exc:
        RUNTIME.failures += 1
        raise HTTPException(
            status_code=422,
            detail={
                "code": "GENERATION_EXHAUSTED",
                "message": str(exc),
                "counts": exc.counts,
            },
        ) from exc
    except ValueError as exc:
        RUNTIME.failures += 1
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_MOLECULE", "message": str(exc)}
        ) from exc
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("MolMIM generation failed")
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
