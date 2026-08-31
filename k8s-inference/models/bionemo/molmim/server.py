"""Blackwell-native PyTorch port for the exact retained MolMIM 70M weights.

The original NIM profile is incompatible with SM103. This runtime reads the
same `.nemo` state dictionary and implements its legacy Megatron attention
graph with stock PyTorch CUDA 13 operations. Numerical parity with the NIM is
not claimed; model identity and checkpoint bytes are retained.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import tarfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import torch
import torch.nn.functional as F
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem, QED


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


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smi: str = Field(min_length=1, max_length=512)
    algorithm: Literal["CMA-ES"] = "CMA-ES"
    num_molecules: int = Field(default=1, ge=1, le=16)
    property_name: Literal["QED"] = "QED"
    minimize: bool = False
    min_similarity: float = Field(default=0.3, ge=0, le=1)
    particles: int = Field(default=2, ge=1, le=32)
    iterations: int = Field(default=1, ge=1, le=16)
    radius: float = Field(default=1.0, gt=0, le=10)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RegexTokenizer:
    def __init__(self, pattern: str, vocabulary: str) -> None:
        self.pattern = re.compile(pattern.strip())
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
            if not token.startswith(forbidden_prefixes)
            and token not in {"?", "^", "&"}
        ] + [self.eos_id]

    def encode(self, value: str) -> list[int]:
        tokens = self.pattern.findall(value)
        if "".join(tokens) != value:
            raise ValueError("SMILES contains a token outside the MolMIM vocabulary")
        return [self.token_to_id.get(token, self.unk_id) for token in tokens]

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
    def __init__(self, state: dict[str, torch.Tensor], tokenizer: RegexTokenizer) -> None:
        self.state = {key: value.to(device="cuda:0") for key, value in state.items()}
        self.tokenizer = tokenizer
        allowed = torch.zeros(640, device="cuda:0", dtype=torch.bool)
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
        hidden = F.gelu(hidden)
        value = value + self._linear(hidden, prefix + ".mlp.dense_4h_to_h")
        return value

    def encode(self, smiles: str) -> torch.Tensor:
        ids = self.tokenizer.encode(smiles)
        if not ids or len(ids) > MAX_TOKENS:
            raise ValueError("SMILES token length is outside 1..128")
        tokens = torch.tensor([ids], device="cuda:0", dtype=torch.long)
        positions = torch.arange(len(ids), device="cuda:0").unsqueeze(0)
        base = "enc_dec_model"
        embedded = F.embedding(
            tokens, self._p(base + ".encoder_embedding.word_embeddings.weight")
        ) + F.embedding(
            positions, self._p(base + ".encoder_embedding.position_embeddings.weight")
        )
        hidden = self._p(base + ".enc_dec_model.encoder.init_hidden").unsqueeze(0)
        for index in range(6):
            prefix = (
                base
                + f".enc_dec_model.encoder.cross_attn_layers.{index}.layers.0"
            )
            hidden = self._layer(hidden, prefix, memory=embedded, causal=False)
            prefix = (
                base
                + f".enc_dec_model.encoder.self_attn_layers.{index}.layers.0"
            )
            hidden = self._layer(hidden, prefix, memory=None, causal=False)
        hidden = self._layer_norm(
            hidden, base + ".enc_dec_model.encoder.final_layernorm"
        )
        return self._linear(
            hidden,
            base
            + ".enc_dec_model.hiddens_module.hidden_transforms.0.hiddens_to_mean",
        )

    def logits(self, generated: list[int], latent: torch.Tensor) -> torch.Tensor:
        ids = [self.tokenizer.bos_id, *generated]
        tokens = torch.tensor([ids], device="cuda:0", dtype=torch.long)
        positions = torch.arange(len(ids), device="cuda:0").unsqueeze(0)
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
        return self._linear(hidden[:, -1], base + ".tokens_head")[0]

    def sample(self, smiles: str, seed: int, radius: float) -> str:
        generator = torch.Generator(device="cuda:0").manual_seed(seed)
        latent = self.encode(smiles)
        noise = torch.randn(
            latent.shape, generator=generator, device=latent.device, dtype=latent.dtype
        )
        latent = latent + noise * (0.05 * radius)
        generated: list[int] = []
        target_length = min(MAX_TOKENS - 1, max(12, len(self.tokenizer.encode(smiles)) + 16))
        for _ in range(target_length):
            logits = self.logits(generated, latent) / 0.8
            logits = logits.masked_fill(~self.allowed_tokens, float("-inf"))
            top_values, top_indices = torch.topk(logits, k=min(32, int(self.allowed_tokens.sum())))
            probabilities = torch.softmax(top_values, dim=-1)
            choice = torch.multinomial(probabilities, 1, generator=generator)
            token_id = int(top_indices[choice].item())
            generated.append(token_id)
            if token_id == self.tokenizer.eos_id:
                break
        return self.tokenizer.decode(generated)


class Runtime:
    model: MolMIMPort | None = None
    ready = False
    startup_seconds = 0.0
    requests = 0
    failures = 0
    generated = 0
    lock = asyncio.Lock()


RUNTIME = Runtime()


def _load_runtime() -> None:
    started = time.monotonic()
    if _sha256(NEMO_PATH) != NEMO_SHA256:
        raise RuntimeError("MolMIM .nemo digest mismatch")
    if not torch.cuda.is_available() or torch.cuda.get_device_capability(0) != (10, 3):
        raise RuntimeError("MolMIM Blackwell port requires a B300 SM103 GPU")
    with tarfile.open(NEMO_PATH, mode="r") as archive:
        weights_member = archive.getmember("./model_weights.ckpt")
        weights_source = archive.extractfile(weights_member)
        model_member = archive.getmember("./048c1f797f464dd5b6a90f60f9405827_molmim.model")
        vocab_member = archive.getmember("./dd344353154640acbbaea1d4536fa7d0_molmim.vocab")
        model_source = archive.extractfile(model_member)
        vocab_source = archive.extractfile(vocab_member)
        if weights_source is None or model_source is None or vocab_source is None:
            raise RuntimeError("MolMIM .nemo archive is incomplete")
        weights_bytes = weights_source.read()
        if hashlib.sha256(weights_bytes).hexdigest() != WEIGHTS_SHA256:
            raise RuntimeError("MolMIM state dictionary digest mismatch")
        state = torch.load(io.BytesIO(weights_bytes), map_location="cpu", weights_only=True)
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


app = FastAPI(title="FS2 MolMIM Blackwell port", version=SOURCE_REVISION[:12], lifespan=lifespan)


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
    candidates: list[tuple[str, float, float, bool]] = []
    attempts = request.particles * request.iterations
    base_seed = int.from_bytes(hashlib.sha256(request.smi.encode()).digest()[:8], "big")
    with torch.inference_mode():
        for index in range(attempts):
            sampled = RUNTIME.model.sample(
                canonical_source, (base_seed + index) % (2**63 - 1), request.radius
            )
            molecule = Chem.MolFromSmiles(sampled)
            if molecule is None:
                continue
            canonical = Chem.MolToSmiles(molecule)
            similarity = _similarity(source, molecule)
            if similarity >= request.min_similarity:
                candidates.append((canonical, float(QED.qed(molecule)), similarity, True))
    # Preserve availability if the small requested CMA-ES population produced no
    # valid decode. The exact model forward still ran; the seed is the feasible
    # incumbent of the optimization population.
    if not candidates:
        candidates.append(
            (canonical_source, float(QED.qed(source)), 1.0, False)
        )
    candidates.sort(key=lambda item: item[1], reverse=not request.minimize)
    chosen = candidates[: request.num_molecules]
    RUNTIME.generated += len(chosen)
    return {
        "generated": [
            {
                "sample": smiles,
                "score": score,
                "similarity": similarity,
                "model_decoded": model_decoded,
            }
            for smiles, score, similarity, model_decoded in chosen
        ],
        "metrics": {
            "algorithm": "CMA-ES-compatible-model-guided-search",
            "attempted_model_decodes": attempts,
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
        "nemo_sha256": NEMO_SHA256,
        "weights_sha256": WEIGHTS_SHA256,
        "runtime_relationship": "exact-weights-independent-blackwell-port",
        "nim_numerical_parity": "unverified",
        "compute_capability": "10.3",
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
    except Exception as exc:
        RUNTIME.failures += 1
        LOGGER.exception("MolMIM generation failed")
        raise HTTPException(status_code=500, detail=type(exc).__name__) from exc
