#!/usr/bin/env python3
"""Generate canonical manifests and the acquisition catalog from reviewed locks.

This file contains metadata only. It never downloads or embeds model bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
GENERATED_AT = "2026-09-02T00:00:00Z"
PROTENIX_V2_SOURCE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
PROTENIX_V2_MIRROR_REVISION = "653edab28103133512575365130916e3fd23ecc3"
PROTENIX_V2_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
PROTENIX_V2_MD5 = "49016ebf4775bf6b629bc4dc77b6673e"
PROTENIX_V2_BYTES = 1859785497
PROTENIX_V2_COMMON_REVISION = "tos-common-2026-01-29"
PROTENIX_V2_COMMON_ARCHIVE_URL = "https://protenix.tos-cn-beijing.volces.com/common.tar.gz"
PROTENIX_V2_COMMON_ARCHIVE_BYTES = 475085654
PROTENIX_V2_COMMON_ARCHIVE_SHA256 = "08ea594f429df35494c062e3dfcacaf48fa761e4ea4a8bcb6d5107d211e64dbd"
PROTENIX_V2_ARTIFACT_REVISION = (
    "code-2475421477ab414b571149ad4a875c390ff8a35d_"
    "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
    "common-2026-01-29"
)
PROTENIX_V2_COMPOSITE_URI = "https://github.com/rene-tech/nebius-solutions-library"
PROTENIX_V2_OFFICIAL_URI = (
    "https://protenix.tos-cn-beijing.volces.com/checkpoint/protenix-v2.pt"
)
PROTENIX_V2_MIRROR_URL = (
    "https://huggingface.co/TMF001/protenix-v2-weights/resolve/"
    f"{PROTENIX_V2_MIRROR_REVISION}/protenix-v2.pt"
)
PROTENIX_V2_COMMON_FILES = (
    (
        "common/clusters-by-entity-40.txt",
        21699572,
        "1ab4af905e75b382eda8dec59917dc3608bee0729e36b9e71baf860bbe86850c",
    ),
    (
        "common/components.cif",
        490777362,
        "bb31ae5cf6c8bc669924313077cb4231ee5ffefd3a20118cd14f3ec89f8bb6a5",
    ),
    (
        "common/components.cif.rdkit_mol.pkl",
        142498117,
        "d1cfb71f5993a3ebea7c47877022d7f597bbfbaf86e28a4770e957da6c50cd35",
    ),
    (
        "common/obsolete_release_date.csv",
        134716,
        "a4f3f63ac5d7eebd78b07995cc669b9eccd6f5d8813c9492c9df02868893cf33",
    ),
)
BOLTZGEN_MOLECULES_REVISION = "c3d36fd276e9caf098c75d4113c6d5eb320b1a4c"
BOLTZGEN_MOLECULES_BYTES = 391401102
BOLTZGEN_MOLECULES_SHA256 = "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53"
ALPHAFOLD2_ARCHIVE_URL = "https://storage.googleapis.com/alphafold/alphafold_params_2022-12-06.tar"
ALPHAFOLD2_ARCHIVE_REVISION = "gcs-generation-1670353726160664"
ALPHAFOLD2_ARCHIVE_BYTES = 5587968000
ALPHAFOLD2_ARCHIVE_SHA256 = "36d4b0220f3c735f3296d301152b738c9776d16981d054845a68a1370b26cfe3"
ALPHAFOLD2_TREE_BYTES = 5587956571
ALPHAFOLD2_TREE_ENTRIES = 16
ALPHAFOLD2_TREE_INVENTORY_SHA256 = "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4"
ALPHAFOLD2_BINDCRAFT_TREE_BYTES = 5587959437
ALPHAFOLD2_BINDCRAFT_TREE_ENTRIES = 17
ALPHAFOLD2_BINDCRAFT_TREE_INVENTORY_SHA256 = "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f"
ALPHAFOLD2_BINDCRAFT_MANIFEST_SHA256 = "9d0b7e45378ed707cfc31585f3ae960282dc76f3e2c4f60b545b02dbc728423b"
COLABDESIGN_REVISION = "e31a56fe1d9b4de25c8697f3a28b75892941cc72"
COLABDESIGN_ARCHIVE_NAME = f"ColabDesign-{COLABDESIGN_REVISION}.tar.gz"
COLABDESIGN_ARCHIVE_URL = f"https://github.com/sokrypton/ColabDesign/archive/{COLABDESIGN_REVISION}.tar.gz"
COLABDESIGN_ARCHIVE_BYTES = 50276715
COLABDESIGN_ARCHIVE_SHA256 = "26c948e5e577c65d5b3e908cc11eece435eb0f05729b1e227926d671c463d37f"
COLABDESIGN_VANILLA_TREE_BYTES = 26602793
COLABDESIGN_VANILLA_TREE_INVENTORY_SHA256 = "2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f"
COLABDESIGN_SOLUBLE_TREE_BYTES = 26601241
COLABDESIGN_SOLUBLE_TREE_INVENTORY_SHA256 = "54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def pretty_json(value: Any) -> bytes:
    """Return the exact bytes written by the runtime-tree materializer."""
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def protenix_localization_manifest(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the one runtime-visible Protenix v2 composite tree manifest."""
    return {
        "schema": "fs2.nebius.ai/protenix-v2-composite-artifact/v1",
        "artifact_id": "protenix-v2",
        "revision": PROTENIX_V2_ARTIFACT_REVISION,
        "sources": {
            "code": {"revision": PROTENIX_V2_SOURCE_REVISION},
            "checkpoint": {
                "revision": f"TMF001/protenix-v2-weights@{PROTENIX_V2_MIRROR_REVISION}",
                "bytes": PROTENIX_V2_BYTES,
                "sha256": PROTENIX_V2_SHA256,
                "md5": PROTENIX_V2_MD5,
                "parameter_count": 464442431,
                "verification": "third-party-mirror-verified-not-publisher-byte-compared",
            },
            "common": {
                "revision": PROTENIX_V2_COMMON_REVISION,
                "archive_url": PROTENIX_V2_COMMON_ARCHIVE_URL,
                "archive_bytes": PROTENIX_V2_COMMON_ARCHIVE_BYTES,
                "archive_sha256": PROTENIX_V2_COMMON_ARCHIVE_SHA256,
            },
        },
        "files": files,
    }


def hf(repo: str, revision: str, path: str, size: int, digest: str, target: str | None = None) -> dict[str, Any]:
    return {
        "path": target or path,
        "url": f"https://huggingface.co/{repo}/resolve/{revision}/{path}",
        "bytes": size,
        "sha256": digest,
    }


def url(path: str, origin: str, size: int, digest: str) -> dict[str, Any]:
    return {"path": path, "url": origin, "bytes": size, "sha256": digest}


def github(repo: str, revision: str, path: str, size: int, digest: str, target: str | None = None) -> dict[str, Any]:
    return url(target or path, f"https://raw.githubusercontent.com/{repo}/{revision}/{path}", size, digest)


def declaration(
    artifact_id: str,
    family: str,
    model_id: str,
    source_uri: str,
    revision: str,
    license_id: str,
    consumers: list[str],
    sources: list[dict[str, Any]],
    smoke: str = "checkpoint",
    provenance: dict[str, Any] | None = None,
    *,
    kind: str = "weights",
    mounts: dict[str, dict[str, str]] | None = None,
    localization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "artifact_id": artifact_id,
        "family": family,
        "model_id": model_id,
        "source_uri": source_uri,
        "revision": revision,
        "license_id": license_id,
        "consumers": consumers,
        "sources": sources,
        "offline_smoke": smoke,
        "kind": kind,
        "mounts": mounts or {},
    }
    if provenance is not None:
        result["provenance"] = provenance
    if localization is not None:
        result["localization"] = localization
    return result


def declarations() -> list[dict[str, Any]]:
    complexa = [
        declaration(
            "complexa-protein", "proteina-complexa", "proteina-complexa-protein-target-160m-v1",
            "hf://nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1", "ffed199e32612b98ffa04f4640d34d37b137fca5",
            "NVIDIA-Open-Model-License-2024-06", ["proteina-complexa-protein"],
            [
                hf("nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1", "ffed199e32612b98ffa04f4640d34d37b137fca5", "complexa.ckpt", 2934289381, "589db1741f29838c7961386f6b873087238c72682e56189b89e0ae02610c19e9"),
                hf("nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1", "ffed199e32612b98ffa04f4640d34d37b137fca5", "complexa_ae.ckpt", 4100101779, "35f8865efd269995eeaf1670e1c1085acfe2988c40abdeda8e09a0e15eb40816"),
            ],
        ),
        declaration(
            "complexa-ligand", "proteina-complexa", "proteina-complexa-ligand-target-160m-v1",
            "hf://nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1", "bc90c8b2c701ceb52d5faef72600b6b5be880244",
            "NVIDIA-Open-Model-License-2024-06", ["proteina-complexa-ligand"],
            [
                hf("nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1", "bc90c8b2c701ceb52d5faef72600b6b5be880244", "complexa_ligand.ckpt", 1790554392, "8175213eac5ec6433fed1756d055ce3f867129bfdb033040e1a0560ca558bfe5"),
                hf("nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1", "bc90c8b2c701ceb52d5faef72600b6b5be880244", "complexa_ligand_ae.ckpt", 4100184649, "898da17022cdeaaaea7caace41c8b6fe7bfcb78be4876b0113127eb9bb1527e6"),
            ],
        ),
        declaration(
            "complexa-ame", "proteina-complexa", "proteina-complexa-ame-160m-v1",
            "hf://nvidia/NV-Proteina-Complexa-AME-160M-v1", "9743d749a8754080a32fda857d95579dfa4dabae",
            "NVIDIA-Open-Model-License-2024-06", ["proteina-complexa-ame"],
            [
                hf("nvidia/NV-Proteina-Complexa-AME-160M-v1", "9743d749a8754080a32fda857d95579dfa4dabae", "complexa_ame.ckpt", 1792013880, "d11319693d024d0427abc356a86350a694a1cb7dceb8db642c8041e5a20a9f7b"),
                hf("nvidia/NV-Proteina-Complexa-AME-160M-v1", "9743d749a8754080a32fda857d95579dfa4dabae", "complexa_ame_ae.ckpt", 4100197925, "63b1358c5459e968628094fdc9a2a6a95ac003606fcf7b1a3a21174458a69734"),
            ],
        ),
    ]
    boltz_revision = "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0"
    boltzgen = declaration(
        "boltzgen-checkpoints", "boltzgen", "boltzgen-1", "hf://boltzgen/boltzgen-1", boltz_revision,
        "MIT", ["boltzgen"],
        [
            hf("boltzgen/boltzgen-1", boltz_revision, "boltz2_aff.ckpt", 2061914091, "6dc13d488015666d3c3fdffd29fab54d72e4f2597b654f996cdcf5937feab090"),
            hf("boltzgen/boltzgen-1", boltz_revision, "boltz2_conf_final.ckpt", 2087255089, "525a51ef306da7282a54d23a4a5b91212fc60d0ff6b23b56dd6351de3b387530"),
            hf("boltzgen/boltzgen-1", boltz_revision, "boltzgen1_adherence.ckpt", 1930858014, "ac7078b3dc13064c68e0c3fd542e5bc538c33558bf6607f65e499eb336ca5e5d"),
            hf("boltzgen/boltzgen-1", boltz_revision, "boltzgen1_diverse.ckpt", 1930847192, "360af8bd6e59527ff6ec25dd81253967f3bd3567d200053b10680634751f8e3c"),
            hf("boltzgen/boltzgen-1", boltz_revision, "boltzgen1_ifold.ckpt", 12582656, "dd4cf108c94471bdc3a326b7b180fa3854dc019110fae780208c30b50bd56578"),
            hf("boltzgen/boltzgen-1", boltz_revision, "boltzgen1_structuretrained_small.ckpt", 2210925373, "e2455b6ff5156218ef3999be894d1a8e4574f0531cefcd9d67d9c1d2d015937b"),
        ],
    )
    boltz2_revision = "6fdef46d763fee7fbb83ca5501ccceff43b85607"
    boltz2 = declaration(
        "boltz2-runtime", "mosaic", "boltz2", "hf://boltz-community/boltz-2", boltz2_revision,
        "MIT", ["mosaic"],
        [
            hf("boltz-community/boltz-2", boltz2_revision, "boltz2_aff.ckpt", 2062139170, "dcc5cd3722b1c9eaa34267e4ae32f55cbbf1963f4c19319381ccfa30fdd2ca9e"),
            hf("boltz-community/boltz-2", boltz2_revision, "boltz2_conf.ckpt", 2286561469, "090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1"),
            hf("boltz-community/boltz-2", boltz2_revision, "mols.tar", 1855662080, "39e076d96dbec6b4e86982bbda16f3a53a2a60c9bdc17828d88f6f9a0c7d1fd7"),
        ],
    )
    mosaic_revision = "70fec525423f5f87156a1a957b4a4048f9f8e676"
    mosaic = declaration(
        "mosaic-components", "mosaic", "mosaic-components", "https://github.com/escalante-bio/mosaic", mosaic_revision,
        "MIT", ["mosaic"],
        [
            github("escalante-bio/mosaic", mosaic_revision, "src/mosaic/proteinmpnn/weights/abmpnn.pt", 20060943, "5dca8d551747cffee33ee319724a47cd2e9d9b45132de13d84b511e3381aa125", "abmpnn.pt"),
            github("escalante-bio/mosaic", mosaic_revision, "src/mosaic/proteinmpnn/weights/soluble_v_48_020.pt", 6681301, "7af52d090172c230c7f0e9d21e02203f6b3a38b16db58d3c7a3960e0a9a6e31a", "soluble_v_48_020.pt"),
            github("escalante-bio/mosaic", mosaic_revision, "src/mosaic/proteinmpnn/weights/v_48_020.pt", 6681301, "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd", "v_48_020.pt"),
        ],
    )
    rf_base = "https://files.ipd.uw.edu/pub/RFdiffusion"
    rfdiffusion = declaration(
        "rfdiffusion-checkpoints", "rfdiffusion", "rfdiffusion", "https://github.com/RosettaCommons/RFdiffusion", "9273ef67335acaf91df0150473a274759229cdf6",
        "BSD-3-Clause", ["rfdiffusion"],
        [url(name, f"{rf_base}/{remote}", size, digest) for name, remote, size, digest in [
            ("ActiveSite_ckpt.pt", "5532d2e1f3a4738decd58b19d633b3c3/ActiveSite_ckpt.pt", 483616107, "beca1f672049161df0bc6a2d2523828f19fd9c8a2b449988e246dde42e7ea986"),
            ("Base_ckpt.pt", "6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt", 483616107, "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"),
            ("Base_epoch8_ckpt.pt", "12fc204edeae5b57713c5ad7dcb97d39/Base_epoch8_ckpt.pt", 483616427, "b8e5d57f0b8a8f8cb30779c106b75210b46a914a4d19fb180676ae647f5ae23d"),
            ("Complex_Fold_base_ckpt.pt", "60f09a193fb5e5ccdc4980417708dbab/Complex_Fold_base_ckpt.pt", 483626923, "0ac3b4024aea811078cec41482528291d6d7d7084bf8190ec118f54642fb81a1"),
            ("Complex_base_ckpt.pt", "e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt", 483619179, "76e4e260aefee3b582bd76b77ab95d2592e64f00c51bf344968ab9239f3250bc"),
            ("Complex_beta_ckpt.pt", "f572d396fae9206628714fb2ce00f72e/Complex_beta_ckpt.pt", 483380617, "5a0b1cafc23c60b1aabcec1e49391986ac4fd02cc1b6b4cc41714ca9fe882e9e"),
            ("InpaintSeq_Fold_ckpt.pt", "76d00716416567174cdb7ca96e208296/InpaintSeq_Fold_ckpt.pt", 483626987, "51849c9fe64c16a38fe41c75db76abe044e4d3493926f6cfd29a5bde0331b7cc"),
            ("InpaintSeq_ckpt.pt", "74f51cfb8b440f50d70878e05361d8f0/InpaintSeq_ckpt.pt", 483619243, "3b71b2b954e87d46b75a88ba64e0420fbf27f592604b10b6c3561b8c8ab70ab6"),
        ]],
    )
    protein_revision = "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57"
    protein_files = [
        ("ca_model_weights/v_48_002.pt", 6624011, "ec038b44a987d7c8351b6ed887c82a2370d54e45e55a6bdaf508a729cef0340e"),
        ("ca_model_weights/v_48_010.pt", 6624011, "cdb50498d45578d20b271fa7817b8cd8bfde3875ad69dbd3f5e4b5dd3e588301"),
        ("ca_model_weights/v_48_020.pt", 6624011, "f28f40170e21858c5ff31ef50b6e63414ff76dc331b19f85aa8586a12031744a"),
        ("soluble_model_weights/v_48_002.pt", 6681301, "0877f840978fe770be6fcec025784d8f50c438571db3260c05e41aa207a7c448"),
        ("soluble_model_weights/v_48_010.pt", 6681301, "79562f7444f72c84595a1c96010713864865a616f4f3967633493041e169fa6e"),
        ("soluble_model_weights/v_48_020.pt", 6681301, "7af52d090172c230c7f0e9d21e02203f6b3a38b16db58d3c7a3960e0a9a6e31a"),
        ("soluble_model_weights/v_48_030.pt", 6681301, "1dd63f1e9fc68a133cc9ef859edf43b489e5ac581cb5624e0b9ec848ff062421"),
        ("vanilla_model_weights/v_48_002.pt", 6681301, "925f2ca1007bf9b02e0e7f420ff00eb91f50fcc2722f64b42e644ae95adaa131"),
        ("vanilla_model_weights/v_48_010.pt", 6681301, "db866fae956a28661f926053d630610c55e9fc4bc03922f2aeeb98a37435ccce"),
        ("vanilla_model_weights/v_48_020.pt", 6681301, "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd"),
        ("vanilla_model_weights/v_48_030.pt", 6681301, "c34b7bfb38418ea30989fda3314f4781ac4e3920f9825731cf555f1fed44ac66"),
    ]
    proteinmpnn = declaration(
        "proteinmpnn-checkpoints", "proteinmpnn", "proteinmpnn", "https://github.com/dauparas/ProteinMPNN", protein_revision,
        "MIT", ["proteina-complexa-protein", "proteina-complexa-ligand", "proteina-complexa-ame", "rfdiffusion", "proteinmpnn"],
        [github("dauparas/ProteinMPNN", protein_revision, path, size, digest) for path, size, digest in protein_files],
    )
    ligand_files = [
        ("global_label_membrane_mpnn_v_48_020.pt", 6751499, "89ef0abddbfb956c4c7c02dcba6523c0b5152733055f7509292365eaecc38b21"),
        ("ligandmpnn_sc_v_32_002_16.pt", 14359379, "799a9de6c1c72bb0c7ceb37998391a6fd0e3e21cb42928ee31a28313a3b1b46a"),
        ("ligandmpnn_v_32_005_25.pt", 10541943, "ee07a7afb53bce98a0b1d33996bbe3b46b0831df5ce0684d3cbd55eed1aa9263"),
        ("ligandmpnn_v_32_010_25.pt", 10541943, "161cd264061fda9680cbb940255522ae42f2966c552d045d87913d9452a80970"),
        ("ligandmpnn_v_32_020_25.pt", 10541943, "42caaa5cdb380867d2b31c30de4ab53ea89171279b3a0732ddf94bc4d3cb6981"),
        ("ligandmpnn_v_32_030_25.pt", 10541943, "ed13e130787f70b6c385efba56949220d16a1d1a98073ada3002b3701a6a8ecf"),
        ("per_residue_label_membrane_mpnn_v_48_020.pt", 6751499, "1e62e193bee12f1b64fff87ae3ca9da95b0f60b15839ae3cbe48e4983ef055ba"),
        ("proteinmpnn_v_48_002.pt", 6681301, "925f2ca1007bf9b02e0e7f420ff00eb91f50fcc2722f64b42e644ae95adaa131"),
        ("proteinmpnn_v_48_010.pt", 6681301, "db866fae956a28661f926053d630610c55e9fc4bc03922f2aeeb98a37435ccce"),
        ("proteinmpnn_v_48_020.pt", 6681301, "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd"),
        ("proteinmpnn_v_48_030.pt", 6681301, "c34b7bfb38418ea30989fda3314f4781ac4e3920f9825731cf555f1fed44ac66"),
        ("solublempnn_v_48_002.pt", 6681301, "0877f840978fe770be6fcec025784d8f50c438571db3260c05e41aa207a7c448"),
        ("solublempnn_v_48_010.pt", 6681301, "79562f7444f72c84595a1c96010713864865a616f4f3967633493041e169fa6e"),
        ("solublempnn_v_48_020.pt", 6681301, "7af52d090172c230c7f0e9d21e02203f6b3a38b16db58d3c7a3960e0a9a6e31a"),
        ("solublempnn_v_48_030.pt", 6681301, "1dd63f1e9fc68a133cc9ef859edf43b489e5ac581cb5624e0b9ec848ff062421"),
    ]
    ligandmpnn = declaration(
        "ligandmpnn-checkpoints", "ligandmpnn", "ligandmpnn", "https://github.com/dauparas/LigandMPNN", "Proteina-Complexa-54058860d434-download-lock",
        "MIT", ["proteina-complexa-protein", "proteina-complexa-ligand", "proteina-complexa-ame"],
        [url(path, f"https://files.ipd.uw.edu/pub/ligandmpnn/{path}", size, digest) for path, size, digest in ligand_files],
    )
    esm_revision = "8fc3ff471022fdce52c77030685eb775de0c00a3"
    esmfold2 = declaration(
        "esmfold2-trunk", "esmfold2", "esmfold2-trunk", "hf://biohub/ESMFold2", esm_revision, "MIT", ["esmfold2"],
        [
            hf("biohub/ESMFold2", esm_revision, "config.json", 2337, "e9ec2496ec433a1dce18627ed4bf3785b4ce0c1d69e4bb4663dad1ab895da012"),
            hf("biohub/ESMFold2", esm_revision, "model.safetensors", 939505228, "138fd4350d6892b81ce6be7ff9bf5a93ae9d4d3751f46a27438a3f9f0dcefa0e"),
        ],
        "esmfold2-model-snapshot",
        mounts={
            "esmfold2": {"mount_root": "/models", "mount_path": "/models/esmfold2"},
        },
    )
    fast_revision = "c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6"
    esmfold2_fast = declaration(
        "esmfold2-fast-trunk", "esmfold2-fast", "esmfold2-fast-trunk", "hf://biohub/ESMFold2-Fast", fast_revision, "MIT", ["esmfold2-fast"],
        [
            hf("biohub/ESMFold2-Fast", fast_revision, "config.json", 2338, "d24456b797ddcfb60ac6c53621b550db5e14b1575ee2d9ab5a380eb5b09902f2"),
            hf("biohub/ESMFold2-Fast", fast_revision, "model.safetensors", 755416924, "60ca19f2898188beba92944365f7b909efd9c99212f5018af75cc47cd9a6184a"),
        ],
        "esmfold2-model-snapshot",
        mounts={
            "esmfold2-fast": {
                "mount_root": "/models",
                "mount_path": "/models/esmfold2-fast",
            },
        },
    )
    esmfold2_ccd = declaration(
        "esmfold2-ccd", "esmfold2", "esmfold2-ccd", "hf://biohub/ESMFold2", esm_revision,
        "MIT", ["esmfold2", "esmfold2-fast"],
        [
            hf("biohub/ESMFold2", esm_revision, "ccd.pkl", 417306584, "9ff44b1927c6b9198e38ffe0928706827a09a350c15530beeeabebfa88038fc5"),
        ],
        "ccd-pickle",
        mounts={
            consumer: {
                "mount_root": "/databases",
                "mount_path": "/databases/esmfold2",
            }
            for consumer in ("esmfold2", "esmfold2-fast")
        },
    )
    esmc_revision = "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a"
    esmc_files = [
        ("config.json", 341, "c5566fab6a17fd674141331fe75de917b7904d99fb7a410d2b1593c21e576913"),
        ("model-00001-of-00006.safetensors", 4864457920, "bd90149ff223e6ac1a0cac6147a5ae0df20d3a21df4f65356a1f19cd14f4aa8a"),
        ("model-00002-of-00006.safetensors", 4971211344, "f75e2144d8269fe2eb4b3e0823fb089b94f176d8024153e85b8fb573a42294fa"),
        ("model-00003-of-00006.safetensors", 4863752992, "f699f01ecc9691d9c6470492765fe54b8b5d2e9f277c139e89427433ffdfe0b2"),
        ("model-00004-of-00006.safetensors", 4971211344, "46add1b7be098bbfdc3073884851ba3057f1b33ea23a158b650a37007dabd13d"),
        ("model-00005-of-00006.safetensors", 4863752992, "1e1cb62f060a34e18f54a31a76683ef888b8cec59e73315f5b31d25d45a1f88c"),
        ("model-00006-of-00006.safetensors", 873762296, "56c73e13ae96e777ce65eee99364056069ef93b646470f352f83c5f1037b1b18"),
        ("model.safetensors.index.json", 97349, "6846456e20e6ee2c37461f7bfc21d316d69bdaf165b925691afcb39e583244da"),
        ("special_tokens_map.json", 171, "0b7245ec86c8c3aeaf61523ba70dfa79be137e6283f127bd651adc30b4f15c74"),
        ("tokenizer.json", 2879, "8d3447b278176e65fb3ef0224472927bf5fee3be46ea2bd77fad0111423cee1f"),
        ("tokenizer_config.json", 1392, "e8d8e40c9f92b334f0272e80bb65ed4043cb9836523cbae899e9859e8cbb8833"),
    ]
    esmc = declaration(
        "esmc-6b", "esmfold2", "esmc-6b", "hf://biohub/ESMC-6B", esmc_revision, "MIT-and-third-party-notices", ["esmfold2", "esmfold2-fast"],
        [hf("biohub/ESMC-6B", esmc_revision, path, size, digest) for path, size, digest in esmc_files], "esmc-6b-snapshot",
    )
    esm2_revision = "08e4846e537177426273712802403f7ba8261b6c"
    esm2 = declaration(
        "esm2-650m", "proteina-complexa", "esm2-t33-650m-ur50d", "hf://facebook/esm2_t33_650M_UR50D", esm2_revision,
        "MIT", ["proteina-complexa-protein", "proteina-complexa-ligand", "proteina-complexa-ame"],
        [hf("facebook/esm2_t33_650M_UR50D", esm2_revision, path, size, digest) for path, size, digest in [
            ("config.json", 724, "539095c22efc52a09d6147074ba4ca119f76a890df5901213b2b55f7d2f96b2b"),
            ("model.safetensors", 2609506392, "a08adabb949fa67ad3c14b509d04fd60368b35007b0095e3358f81200c4f4db0"),
            ("special_tokens_map.json", 125, "3aedcd4211c0d43aec4e607ff60a63255f3174ead795e997350f09a5f8cd9ee1"),
            ("tokenizer_config.json", 95, "7e9161ecdb548ec45a41cbc6b24aa4476fdd418461f491c4207baa99419a29ad"),
            ("vocab.txt", 93, "0b82cc0a7c7cf9e567b1e5892d793285b9fbae822c964ca48696f7db44598e03"),
        ]],
    )
    openfold = declaration(
        "openfold3-openbind-0", "openfold3", "openfold3-openbind-0", "https://openfold3-data.s3.us-west-2.amazonaws.com/openfold3-parameters/of3-ob-2025-06-30-174k.pt", "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
        "Apache-2.0", ["openfold3"],
        [
            url("of3-ob-2025-06-30-174k.pt", "https://openfold3-data.s3.us-west-2.amazonaws.com/openfold3-parameters/of3-ob-2025-06-30-174k.pt", 2287872989, "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4"),
        ],
        mounts={
            "openfold3": {"mount_root": "/models", "mount_path": "/models/openfold3"},
        },
    )
    openfold_components = declaration(
        "openfold3-components-bcif",
        "openfold3",
        "openfold3-components-bcif",
        "https://openfold3-data.s3.us-west-2.amazonaws.com/components.bcif",
        "s3://openfold3-data/components.bcif#etag-b251a30629b9c30d077a5b91aeefecb2-4",
        "CC0-1.0",
        ["openfold3"],
        [
            url(
                "components.bcif",
                "https://openfold3-data.s3.us-west-2.amazonaws.com/components.bcif",
                63393643,
                "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c",
            ),
        ],
        "binarycif-ccd",
        kind="snapshot",
        mounts={
            "openfold3": {
                "mount_root": "/databases",
                "mount_path": "/databases/openfold3",
            },
        },
    )
    protenix_v2 = declaration(
        "protenix-v2",
        "protenix",
        "protenix-v2",
        PROTENIX_V2_COMPOSITE_URI,
        PROTENIX_V2_ARTIFACT_REVISION,
        "Apache-2.0",
        ["protenix-v2"],
        [
            url(
                "checkpoint/protenix-v2.pt",
                PROTENIX_V2_MIRROR_URL,
                PROTENIX_V2_BYTES,
                PROTENIX_V2_SHA256,
            ),
            *[
                url(
                    path,
                    f"https://protenix.tos-cn-beijing.volces.com/{path}",
                    size,
                    digest,
                )
                for path, size, digest in PROTENIX_V2_COMMON_FILES
            ],
        ],
        "protenix-v2-bundle",
        mounts={
            "protenix-v2": {
                "mount_root": "/models",
                "mount_path": "/models/protenix-v2",
            },
        },
        provenance={
            "state": "mirror-verified-not-publisher-byte-compared",
            "canonical_source": {
                "publisher": "ByteDance",
                "uri": PROTENIX_V2_OFFICIAL_URI,
                "source_revision": PROTENIX_V2_SOURCE_REVISION,
                "publisher_bytes_reachable": False,
                "publisher_digest_available": False,
            },
            "acquisition_source": {
                "relationship": "third-party-mirror",
                "repository": "TMF001/protenix-v2-weights",
                "repository_revision": PROTENIX_V2_MIRROR_REVISION,
                "url": PROTENIX_V2_MIRROR_URL,
                "lfs_oid_sha256": PROTENIX_V2_SHA256,
                "bytes": PROTENIX_V2_BYTES,
            },
            "verification": {
                "evidence": "evidence/protenix-v2-mirror-verification-20260902.json",
                "evidence_sha256": "5275cfd9b882d08cf7f3e1f1e77e158be3890937d2b72ab94cdc577e7e2568ae",
                "sha256": PROTENIX_V2_SHA256,
                "md5": PROTENIX_V2_MD5,
                "safe_torch_load": "weights-only-mmap-cpu",
                "root_type": "dict",
                "top_level_key": "model",
                "checkpoint_key_count": 4174,
                "checkpoint_tensor_count": 4174,
                "checkpoint_tensor_dtypes": {"torch.float32": 4174},
                "source_state_key_count": 4174,
                "checkpoint_parameter_count": 464442431,
                "checkpoint_element_count": 464442431,
                "source_parameter_count": 464442431,
                "key_shape_inventory_sha256": "11f1ac80197fe095aa25dba49d6e772076402a744ace1853e3338a9f27c2946b",
                "inspection_image_digest": "sha256:ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240",
                "inspection_torch_version": "2.7.1+cu126",
                "strict_key_shape_match": True,
                "publisher_byte_compared": False,
            },
        },
    )
    boltzgen_molecules = declaration(
        "boltzgen-inference-molecules",
        "boltzgen",
        "boltzgen-inference-molecules",
        "hf://datasets/boltzgen/inference-data",
        BOLTZGEN_MOLECULES_REVISION,
        "MIT",
        ["boltzgen"],
        [
            hf(
                "datasets/boltzgen/inference-data",
                BOLTZGEN_MOLECULES_REVISION,
                "mols.zip",
                BOLTZGEN_MOLECULES_BYTES,
                BOLTZGEN_MOLECULES_SHA256,
            )
        ],
        "boltzgen-molecules-zip",
        kind="snapshot",
        mounts={
            "boltzgen": {
                "mount_root": "/opt/fs2/artifacts",
                "mount_path": "/opt/fs2/artifacts/boltzgen-inference-molecules",
            },
        },
        localization={
            "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "transform": "safe-extract-zip",
            "archive_sha256": BOLTZGEN_MOLECULES_SHA256,
            "mount_paths": ["/opt/fs2/artifacts/boltzgen-inference-molecules"],
            "tree": {
                "entry_count": 45227,
                "total_bytes": 1820698819,
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc",
            },
        },
    )
    alphafold2 = declaration(
        "alphafold2-params", "alphafold2", "alphafold2-params-2022-12-06", ALPHAFOLD2_ARCHIVE_URL, ALPHAFOLD2_ARCHIVE_REVISION,
        "CC-BY-4.0", ["proteina-complexa-protein", "proteina-complexa-ligand", "proteina-complexa-ame"],
        [url("alphafold_params_2022-12-06.tar", ALPHAFOLD2_ARCHIVE_URL, ALPHAFOLD2_ARCHIVE_BYTES, ALPHAFOLD2_ARCHIVE_SHA256)], "archive",
        mounts={
            consumer: {
                "mount_root": "/opt/fs2/artifacts",
                "mount_path": "/opt/fs2/artifacts/alphafold2-params",
            }
            for consumer in (
                "proteina-complexa-protein",
                "proteina-complexa-ligand",
                "proteina-complexa-ame",
            )
        },
        localization={
            "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "transform": "safe-extract-tar",
            "archive_sha256": ALPHAFOLD2_ARCHIVE_SHA256,
            "mount_paths": ["/opt/fs2/artifacts/alphafold2-params"],
            "tree": {
                "entry_count": ALPHAFOLD2_TREE_ENTRIES,
                "total_bytes": ALPHAFOLD2_TREE_BYTES,
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": ALPHAFOLD2_TREE_INVENTORY_SHA256,
            },
        },
    )
    alphafold2_bindcraft = declaration(
        "alphafold2-params-bindcraft", "alphafold2", "alphafold2-params-2022-12-06", ALPHAFOLD2_ARCHIVE_URL, ALPHAFOLD2_ARCHIVE_REVISION,
        "CC-BY-4.0", ["bindcraft"],
        [url("alphafold_params_2022-12-06.tar", ALPHAFOLD2_ARCHIVE_URL, ALPHAFOLD2_ARCHIVE_BYTES, ALPHAFOLD2_ARCHIVE_SHA256)], "archive",
        mounts={
            "bindcraft": {
                "mount_root": "/models",
                "mount_path": "/models/alphafold2",
            },
        },
        localization={
            "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "transform": "safe-extract-tar",
            "archive_sha256": ALPHAFOLD2_ARCHIVE_SHA256,
            "mount_paths": ["/models/alphafold2"],
            "tree": {
                "entry_count": ALPHAFOLD2_BINDCRAFT_TREE_ENTRIES,
                "total_bytes": ALPHAFOLD2_BINDCRAFT_TREE_BYTES,
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": ALPHAFOLD2_BINDCRAFT_TREE_INVENTORY_SHA256,
            },
        },
    )
    colabdesign_vanilla = declaration(
        "colabdesign-mpnn-weights-vanilla", "colabdesign", "colabdesign-mpnn-weights", "https://github.com/sokrypton/ColabDesign", COLABDESIGN_REVISION,
        "Apache-2.0", ["bindcraft"],
        [url(COLABDESIGN_ARCHIVE_NAME, COLABDESIGN_ARCHIVE_URL, COLABDESIGN_ARCHIVE_BYTES, COLABDESIGN_ARCHIVE_SHA256)], "archive",
        kind="snapshot",
        mounts={
            "bindcraft": {
                "mount_root": "/opt/conda/lib/python3.10/site-packages",
                "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
            },
        },
        localization={
            "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "transform": "safe-extract-tar-gz",
            "archive_sha256": COLABDESIGN_ARCHIVE_SHA256,
            "mount_paths": [
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"
            ],
            "tree": {
                "entry_count": 5,
                "total_bytes": COLABDESIGN_VANILLA_TREE_BYTES,
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": COLABDESIGN_VANILLA_TREE_INVENTORY_SHA256,
            },
        },
    )
    colabdesign_soluble = declaration(
        "colabdesign-mpnn-weights-soluble", "colabdesign", "colabdesign-mpnn-weights", "https://github.com/sokrypton/ColabDesign", COLABDESIGN_REVISION,
        "Apache-2.0", ["bindcraft"],
        [url(COLABDESIGN_ARCHIVE_NAME, COLABDESIGN_ARCHIVE_URL, COLABDESIGN_ARCHIVE_BYTES, COLABDESIGN_ARCHIVE_SHA256)], "archive",
        kind="snapshot",
        mounts={
            "bindcraft": {
                "mount_root": "/opt/conda/lib/python3.10/site-packages",
                "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
            },
        },
        localization={
            "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "transform": "safe-extract-tar-gz",
            "archive_sha256": COLABDESIGN_ARCHIVE_SHA256,
            "mount_paths": [
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble"
            ],
            "tree": {
                "entry_count": 5,
                "total_bytes": COLABDESIGN_SOLUBLE_TREE_BYTES,
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": COLABDESIGN_SOLUBLE_TREE_INVENTORY_SHA256,
            },
        },
    )
    rosettafold3 = declaration(
        "rosettafold3-checkpoint", "rosettafold3", "rosettafold3-foundry-2024-01", "https://files.ipd.uw.edu/pub/rf3/rf3_foundry_01_24_latest_remapped.ckpt", "foundry-production-b02eed6a-checksum-lock",
        "BSD-3-Clause", ["proteina-complexa-protein", "proteina-complexa-ligand", "proteina-complexa-ame"],
        [url("rf3_foundry_01_24_latest_remapped.ckpt", "https://files.ipd.uw.edu/pub/rf3/rf3_foundry_01_24_latest_remapped.ckpt", 3038876446, "364ef592fd8042a9cf4176d045015190f8322f961ccca38d891b20ca578d3bb0")],
    )
    return complexa + [
        boltzgen, boltzgen_molecules, boltz2, mosaic, rfdiffusion, proteinmpnn, ligandmpnn,
        esmfold2, esmfold2_fast, esmfold2_ccd, esmc, esm2, openfold,
        openfold_components, protenix_v2,
        alphafold2, alphafold2_bindcraft, colabdesign_vanilla,
        colabdesign_soluble, rosettafold3,
    ]


def blocked_entries() -> dict[str, dict[str, Any]]:
    return {
        "alphafold3-private": {
            "id": "alphafold3-private", "family": "alphafold3", "state": "excluded-private",
            "reason": "AlphaFold3 parameters are governed by separate non-commercial terms and remain in the owner-only academic-assets quarantine; generic multi-tenant cache publication is prohibited.",
            "consumers": ["alphafold3"],
        },
        "pyrosetta-private": {
            "id": "pyrosetta-private", "family": "pyrosetta", "state": "excluded-private",
            "reason": "PyRosetta requires organization-scoped academic licensing and remains a private runtime layer; it must never enter the generic multi-tenant artifact cache.",
            "consumers": ["bindcraft-pyrosetta"],
        },
    }


def documents() -> dict[Path, Any]:
    academic_contract = json.loads(
        (ROOT.parent / "academic-assets/contracts/academic-assets.json").read_text()
    )
    academic_evidence = json.loads(
        (ROOT.parent / "academic-assets/evidence/live-acceptance-state.json").read_text()
    )
    pyrosetta_contract = academic_contract["assets"]["pyrosetta-bindcraft"]
    pyrosetta_binding = pyrosetta_contract["delivery"]["runtime_binding"]
    pyrosetta_tree = academic_evidence["semantic_evidence"]["installed_tree"]
    protenix_files = sorted(
        [
            {
                "path": "checkpoint/protenix-v2.pt",
                "bytes": PROTENIX_V2_BYTES,
                "sha256": PROTENIX_V2_SHA256,
            },
            *[
                {"path": path, "bytes": size, "sha256": digest}
                for path, size, digest in PROTENIX_V2_COMMON_FILES
            ],
        ],
        key=lambda item: item["path"],
    )
    protenix_source_content_digest = hashlib.sha256(canonical(protenix_files)).hexdigest()
    protenix_localization = protenix_localization_manifest(protenix_files)
    protenix_localization_digest = hashlib.sha256(canonical(protenix_localization)).hexdigest()
    protenix_manifest_bytes = pretty_json(protenix_localization)
    protenix_marker_bytes = f"{protenix_localization_digest}\n".encode("ascii")
    protenix_localized_files = sorted(
        [
            *protenix_files,
            {
                "path": "manifest.json",
                "bytes": len(protenix_manifest_bytes),
                "sha256": hashlib.sha256(protenix_manifest_bytes).hexdigest(),
            },
            {
                "path": ".fs2-manifest-sha256",
                "bytes": len(protenix_marker_bytes),
                "sha256": hashlib.sha256(protenix_marker_bytes).hexdigest(),
            },
        ],
        key=lambda item: item["path"],
    )
    protenix_localized_content_digest = hashlib.sha256(
        canonical(protenix_localized_files)
    ).hexdigest()
    catalog: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/public-artifact-catalog/v1",
        "generated_at": GENERATED_AT,
        "licenses": {
            "Apache-2.0": {"id": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0", "commercial_use": "permitted", "redistribution": "permitted-with-notice"},
            "BSD-3-Clause": {"id": "BSD-3-Clause", "url": "https://opensource.org/license/bsd-3-clause", "commercial_use": "permitted", "redistribution": "permitted-with-notice"},
            "CC-BY-4.0": {"id": "CC-BY-4.0", "url": "https://creativecommons.org/licenses/by/4.0/legalcode", "commercial_use": "permitted", "redistribution": "permitted-with-attribution"},
            "CC0-1.0": {"id": "CC0-1.0", "url": "https://www.wwpdb.org/about/usage-policies", "commercial_use": "permitted", "redistribution": "public-domain-dedication"},
            "MIT": {"id": "MIT", "url": "https://opensource.org/license/mit", "commercial_use": "permitted", "redistribution": "permitted-with-notice"},
            "MIT-and-third-party-notices": {"id": "MIT-and-third-party-notices", "url": "https://huggingface.co/biohub/ESMC-6B/blob/45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a/README.md", "commercial_use": "permitted-subject-to-component-notices", "redistribution": "permitted-subject-to-component-notices"},
            "NVIDIA-Open-Model-License-2024-06": {"id": "NVIDIA-Open-Model-License-2024-06", "url": "https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/", "commercial_use": "permitted", "redistribution": "permitted-with-attribution-and-license-copy"},
        },
        "artifacts": {},
        "consumers": {},
        "consumer_layouts": {},
        "private_layouts": {
            "alphafold3": {
                "artifact_id": "alphafold3-private",
                "source_plane": "academic-assets",
                "cache_scope": "tenant-private",
                "general_shared_cache_allowed": False,
                "embed_in_image": False,
                "source_url": "https://storage.googleapis.com/alphafold3/af3.bin.zst",
                "source_revision": "gs://alphafold3/af3.bin.zst#1780568696389861",
                "generation": "1780568696389861",
                "last_modified": "2026-06-04T10:24:56Z",
                "filename": "af3.bin.zst",
                "bytes": 1020545840,
                "sha256": "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
                "mount_path": "/models",
                "file_path": "/models/af3.bin.zst",
                "read_only": True,
                "runtime_argument": "--model_dir=/models",
                "evidence": "evidence/esm-af3-external-runtime-contract-20260902.json",
            },
        },
        "reference_layouts": {
            "alphafold3": {
                "bundle_id": "alphafold3-public-databases-v3.0",
                "bundle_revision": "v3.0-paper-snapshot-2022-09-28",
                "source_plane": "reference-data",
                "source_revision": "231efc9bb9c13b45cc59e43f7107869084ee9624",
                "mount_path": "/databases",
                "read_only": True,
                "runtime_argument": "--db_dir=/databases",
            },
        },
        "runtime_constraints": {
            consumer_id: {
                "binary_compatibility": "candidate-hopper-sm90-cubin-no-ptx",
                "candidate_accelerator_families": ["Hopper"],
                "candidate_cuda_architectures": ["sm90"],
                "qualification_state": "pending-exact-image-h100-semantic-test",
                "blackwell_state": "blocked",
                "blackwell_unblock_requires_one_of": [
                    "qualified-sdpa-fallback-image",
                    "target-aware-blackwell-image",
                ],
                "external_immutable_artifacts": ["esmc-6b", "esmfold2-ccd"],
                "binary_evidence": {
                    "component": "flash-attn",
                    "version": "2.7.4.post1",
                    "wheel": "flash_attn-2.7.4.post1-cp312-cp312-linux_x86_64.whl",
                    "wheel_url": "https://github.com/evolutionaryscale/wheels/releases/download/py312-pt211-cu13-sm80-90/flash_attn-2.7.4.post1-cp312-cp312-linux_x86_64.whl",
                    "source_revision": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
                    "native_cubins": ["sm80", "sm90"],
                    "ptx_present": False,
                    "inspection": "manager-provided-frozen-wheel-binary-inspection",
                },
                "evidence": "evidence/esm-af3-external-runtime-contract-20260902.json",
            }
            for consumer_id in ("esmfold2", "esmfold2-fast")
        },
        "runtime_handoffs": {
            "protenix-v2": {
                "schema": "fs2-serve.nebius.ai/public-artifact-runtime-handoff/v1",
                "model_id": "protenix-v2",
                "variant_id": "upstream-v2-0-0",
                "source": {
                    "repository": "https://github.com/bytedance/Protenix",
                    "revision": PROTENIX_V2_SOURCE_REVISION,
                    "tag": "v2.0.0",
                    "model_name": "protenix-v2",
                },
                "checkpoint": {
                    "artifact_id": "protenix-v2",
                    "source_path": "checkpoint/protenix-v2.pt",
                    "mount_path": "/models/protenix-v2",
                    "runtime_path": "/models/protenix-v2/checkpoint/protenix-v2.pt",
                    "bytes": PROTENIX_V2_BYTES,
                    "sha256": PROTENIX_V2_SHA256,
                    "md5": PROTENIX_V2_MD5,
                    "provenance_state": "mirror-verified-not-publisher-byte-compared",
                    "mirror_repository": "TMF001/protenix-v2-weights",
                    "mirror_revision": PROTENIX_V2_MIRROR_REVISION,
                },
                "localization": {
                    "artifact_id": "protenix-v2",
                    "mount_path": "/models/protenix-v2",
                    "manifest_path": "/models/protenix-v2/manifest.json",
                    "manifest_schema": "fs2.nebius.ai/protenix-v2-composite-artifact/v1",
                    "manifest_sha256": protenix_localization_digest,
                    "ready_marker_path": "/models/protenix-v2/.fs2-manifest-sha256",
                    "source_content_digest_sha256": protenix_source_content_digest,
                    "content_digest_sha256": protenix_localized_content_digest,
                    "files": protenix_localized_files,
                    "required_files": [item["path"] for item in protenix_localized_files],
                },
                "image": {
                    "runtime_id": "protenix-v2",
                    "source_revision": PROTENIX_V2_SOURCE_REVISION,
                    "candidate_tag": f"{PROTENIX_V2_SOURCE_REVISION}-h100-r2",
                    "runtime_base_image": (
                        "pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime@sha256:"
                        "2b59b1b91885677814f78be1f8df48a25d5dc952eb6580eaecfefca510f9afd3"
                    ),
                    "entrypoint": "/usr/local/bin/fs2-run-protenix",
                    "required_checkpoint_path": "/models/protenix-v2/checkpoint/protenix-v2.pt",
                    "required_manifest_path": "/models/protenix-v2/manifest.json",
                    "required_manifest_sha256": protenix_localization_digest,
                    "required_ready_marker_path": "/models/protenix-v2/.fs2-manifest-sha256",
                    "required_content_sha256": protenix_localized_content_digest,
                    "digest_required": True,
                    "known_unqualified_digests": [
                        "sha256:ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240"
                    ],
                },
                "adapter": {
                    "model_id": "protenix-v2",
                    "variant_id": "upstream-v2-0-0",
                    "artifact_id": "protenix-v2",
                    "required_files": [item["path"] for item in protenix_localized_files],
                    "mount_path": "/models/protenix-v2",
                    "expected_content_sha256": protenix_localized_content_digest,
                    "expected_manifest_sha256": protenix_localization_digest,
                    "forbidden_artifact_ids": ["protenix-v1-substitute"],
                },
                "semantic_smoke": {
                    "state": "required-not-yet-qualified",
                    "target": {
                        "project_id": "${PROJECT_ID}",
                        "region": "eu-north1",
                        "cluster_context": "k8s-inference-h100",
                        "accelerator_product": "NVIDIA-H100-80GB-HBM3",
                        "compute_capability": "9.0",
                    },
                    "fixture": {
                        "path": "smoke/protenix-v2-minimal.json",
                        "sha256": "616cbd11f2c07e57c4e0c6ac121bfc891e593ec072946a577f921993c2e9f50e",
                    },
                    "network_mode": "offline",
                    "preprocessing": {
                        "namespace": "fs2-reference-data",
                        "local_queue": "reference-data",
                        "pool": "reference-data",
                        "artifact_id": "protenix-v2",
                        "mount_path": "/models/protenix-v2",
                    },
                    "stages": [
                        {
                            "id": "prepare-data",
                            "compute": "dedicated-cpu-preprocessing",
                            "argv": [
                                "/usr/local/bin/fs2-run-protenix",
                                "prep",
                                "--input",
                                "/work/input.json",
                                "--output-dir",
                                "/work/prepared",
                                "--processed-json",
                                "/work/prepared/input.json",
                                "--msa-mode",
                                "none",
                            ],
                        },
                        {
                            "id": "sample-structure",
                            "compute": "h100-sm90",
                            "argv": [
                                "/usr/local/bin/fs2-run-protenix",
                                "pred",
                                "--input",
                                "/work/prepared/input.json",
                                "--output-dir",
                                "/work/output",
                                "--msa-mode",
                                "none",
                                "--seeds",
                                "101",
                                "--cycle",
                                "10",
                                "--step",
                                "200",
                                "--sample",
                                "5",
                            ],
                        },
                    ],
                    "required_evidence": [
                        "corrected-image-repository-digest",
                        "public-cache-receipt",
                        "composite-localization-receipt",
                        "observed-h100-sm90",
                        "offline-egress-enforcement",
                        "parseable-mmcif",
                        "finite-confidence-json",
                        "semantic-validator-pass",
                    ],
                },
            }
        },
    }
    output: dict[Path, Any] = {}
    manifests: dict[str, dict[str, Any]] = {}
    mount_overrides: dict[tuple[str, str], dict[str, str]] = {}
    for item in declarations():
        sources = sorted(item["sources"], key=lambda source: source["path"])
        files = [{key: source[key] for key in ("path", "bytes", "sha256")} for source in sources]
        manifest = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": item["model_id"],
            "kind": item["kind"],
            "source": {"uri": item["source_uri"], "revision": item["revision"]},
            "content": {
                "digest": hashlib.sha256(canonical(files)).hexdigest(),
                "expanded_bytes": sum(source["bytes"] for source in sources),
                "files": files,
            },
            "license": {"id": item["license_id"], "state": "verified"},
            "entitlement_state": "not-required",
            "owner": "cancer-immunotherapy",
            "retention": "retained-platform",
        }
        manifest_name = f"manifest-{item['artifact_id']}.json"
        output[ROOT / manifest_name] = manifest
        manifests[item["artifact_id"]] = manifest
        catalog_entry = {
            "id": item["artifact_id"], "family": item["family"], "state": "available", "reason": None,
            "consumers": sorted(item["consumers"]), "manifest": manifest_name,
            "sources": sources, "offline_smoke": item["offline_smoke"],
        }
        if "provenance" in item:
            catalog_entry["provenance"] = item["provenance"]
        if "localization" in item:
            catalog_entry["localization"] = item["localization"]
        catalog["artifacts"][item["artifact_id"]] = catalog_entry
        for consumer in item["consumers"]:
            catalog["consumers"].setdefault(consumer, []).append(item["artifact_id"])
            if consumer in item["mounts"]:
                mount_overrides[(consumer, item["artifact_id"])] = item["mounts"][consumer]
    for artifact_id, entry in blocked_entries().items():
        catalog["artifacts"][artifact_id] = entry
        for consumer in entry["consumers"]:
            catalog["consumers"].setdefault(consumer, []).append(artifact_id)
    catalog["artifacts"] = dict(sorted(catalog["artifacts"].items()))
    catalog["consumers"] = {key: sorted(value) for key, value in sorted(catalog["consumers"].items())}
    for consumer_id, artifact_ids in catalog["consumers"].items():
        bindings = []
        for artifact_id in artifact_ids:
            if catalog["artifacts"][artifact_id]["state"] != "available":
                continue
            override = mount_overrides.get((consumer_id, artifact_id), {})
            binding = {
                "artifact_id": artifact_id,
                "mount_path": override.get("mount_path", f"/models/{artifact_id}"),
                "read_only": True,
            }
            if override.get("mount_root", "/models") != "/models":
                binding["mount_root"] = override["mount_root"]
            bindings.append(binding)
        if bindings:
            layout = {
                "mount_root": "/models",
                "bindings": bindings,
            }
            if consumer_id == "openfold3":
                layout["runtime_paths"] = {
                    "checkpoint": "/models/openfold3/of3-ob-2025-06-30-174k.pt",
                    "components_bcif": "/databases/openfold3/components.bcif",
                }
            elif consumer_id == "esmfold2":
                layout["runtime_paths"] = {
                    "model_dir": "/models/esmfold2",
                    "esmc_dir": "/models/esmc-6b",
                    "ccd_path": "/databases/esmfold2/ccd.pkl",
                }
            elif consumer_id == "esmfold2-fast":
                layout["runtime_paths"] = {
                    "model_dir": "/models/esmfold2-fast",
                    "esmc_dir": "/models/esmc-6b",
                    "ccd_path": "/databases/esmfold2/ccd.pkl",
                }
            elif consumer_id == "boltzgen":
                layout["runtime_paths"] = {
                    "molecules_archive": (
                        "/opt/fs2/artifacts/boltzgen-inference-molecules/mols.zip"
                    ),
                    "moldir": "/opt/fs2/artifacts/boltzgen-inference-molecules",
                }
            elif consumer_id == "protenix-v2":
                layout["runtime_paths"] = {
                    "checkpoint": "/models/protenix-v2/checkpoint/protenix-v2.pt",
                }
            catalog["consumer_layouts"][consumer_id] = layout
    integration = {
        "schema": "fs2-serve.nebius.ai/public-artifact-runtime-integration/v1",
        "generated_at": GENERATED_AT,
        "consumers": {
            "esmfold2": {
                "artifacts": {
                    artifact_id: {
                        "content_digest_sha256": manifests[artifact_id]["content"]["digest"],
                        "mount_path": next(
                            binding["mount_path"]
                            for binding in catalog["consumer_layouts"]["esmfold2"]["bindings"]
                            if binding["artifact_id"] == artifact_id
                        ),
                    }
                    for artifact_id in ("esmfold2-trunk", "esmc-6b", "esmfold2-ccd")
                },
                "runtime_paths": catalog["consumer_layouts"]["esmfold2"]["runtime_paths"],
                "accelerator_compatibility": "binary-compatible-hopper-candidate-sm90-no-ptx",
                "qualification_state": "pending-exact-image-h100-semantic-test",
            },
            "esmfold2-fast": {
                "artifacts": {
                    artifact_id: {
                        "content_digest_sha256": manifests[artifact_id]["content"]["digest"],
                        "mount_path": next(
                            binding["mount_path"]
                            for binding in catalog["consumer_layouts"]["esmfold2-fast"]["bindings"]
                            if binding["artifact_id"] == artifact_id
                        ),
                    }
                    for artifact_id in ("esmfold2-fast-trunk", "esmc-6b", "esmfold2-ccd")
                },
                "runtime_paths": catalog["consumer_layouts"]["esmfold2-fast"]["runtime_paths"],
                "accelerator_compatibility": "binary-compatible-hopper-candidate-sm90-no-ptx",
                "qualification_state": "pending-exact-image-h100-semantic-test",
            },
            "protenix-v2": {
                "artifact_id": "protenix-v2",
                "source_content_digest_sha256": protenix_source_content_digest,
                "localized_content_digest_sha256": protenix_localized_content_digest,
                "localization_manifest_sha256": protenix_localization_digest,
                "mount_path": "/models/protenix-v2",
                "checkpoint_path": "/models/protenix-v2/checkpoint/protenix-v2.pt",
                "required_files": [item["path"] for item in protenix_localized_files],
                "localization_command": (
                    "python3 model-artifacts/public_artifacts.py materialize "
                    "--artifact protenix-v2 --catalog <catalog> "
                    "--cache-root <cache-root> --destination /models/protenix-v2"
                ),
                "forbidden_legacy_artifacts": [
                    "protenix-v1-substitute",
                    "protenix-v2-inference-data-2026-01-29",
                ],
            },
            "openfold3": {
                "artifacts": {
                    "openfold3-openbind-0": {
                        "content_digest_sha256": manifests["openfold3-openbind-0"]["content"]["digest"],
                        "file_sha256": "bd43301c011d5f87580d3e8b548658869433e4488399feb03035ba248f8e29e4",
                        "mount_path": "/models/openfold3",
                    },
                    "openfold3-components-bcif": {
                        "content_digest_sha256": manifests["openfold3-components-bcif"]["content"]["digest"],
                        "file_sha256": "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c",
                        "mount_path": "/databases/openfold3",
                    },
                },
                "runtime_paths": catalog["consumer_layouts"]["openfold3"]["runtime_paths"],
            },
            "boltzgen": {
                "artifact_id": "boltzgen-inference-molecules",
                "content_digest_sha256": manifests["boltzgen-inference-molecules"]["content"]["digest"],
                "archive": {
                    "path": "mols.zip",
                    "bytes": BOLTZGEN_MOLECULES_BYTES,
                    "sha256": BOLTZGEN_MOLECULES_SHA256,
                    "revision": BOLTZGEN_MOLECULES_REVISION,
                    "entry_count": 45227,
                    "expanded_bytes": 1820698819,
                    "expanded_inventory_sha256": (
                        "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc"
                    ),
                    "path_contract": "45227-flat-root-pkl-files",
                    "central_directory": {
                        "offset": 387915761,
                        "bytes": 3485319,
                    },
                },
                "localization": {
                    "transform": "safe-extract-zip",
                    "destination": "/opt/fs2/artifacts/boltzgen-inference-molecules",
                    "runtime_argument": (
                        "--moldir=/opt/fs2/artifacts/boltzgen-inference-molecules"
                    ),
                    "receipt_must_bind_source_content_digest": True,
                    "receipt_must_bind_expanded_inventory_sha256": (
                        "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc"
                    ),
                },
            },
            "proteina-complexa": {
                "artifact_id": "alphafold2-params",
                "source_content_digest_sha256": manifests["alphafold2-params"]["content"]["digest"],
                "archive": {
                    "path": "alphafold_params_2022-12-06.tar",
                    "bytes": ALPHAFOLD2_ARCHIVE_BYTES,
                    "sha256": ALPHAFOLD2_ARCHIVE_SHA256,
                    "revision": ALPHAFOLD2_ARCHIVE_REVISION,
                },
                "localization": {
                    "transform": "safe-extract-tar",
                    "destination": "/opt/fs2/artifacts/alphafold2-params",
                    "runtime_binding": "AF2_DIR=/opt/fs2/artifacts/alphafold2-params",
                    "tree_entry_count": ALPHAFOLD2_TREE_ENTRIES,
                    "tree_total_bytes": ALPHAFOLD2_TREE_BYTES,
                    "tree_inventory_algorithm": "fs2-flat-tree-inventory/v1",
                    "tree_inventory_sha256": ALPHAFOLD2_TREE_INVENTORY_SHA256,
                    "tree_entry_pattern": "^(LICENSE|params_model_[1-5](_ptm|_multimer_v3)?\\.npz)$",
                    "receipt_must_bind_source_content_digest": True,
                },
            },
            "bindcraft": {
                "artifacts": {
                    "alphafold2-params-bindcraft": {
                        "source_content_digest_sha256": manifests["alphafold2-params-bindcraft"]["content"]["digest"],
                        "archive_sha256": ALPHAFOLD2_ARCHIVE_SHA256,
                        "mount_path": "/models/alphafold2",
                        "tree_entry_count": ALPHAFOLD2_BINDCRAFT_TREE_ENTRIES,
                        "tree_total_bytes": ALPHAFOLD2_BINDCRAFT_TREE_BYTES,
                        "tree_inventory_sha256": ALPHAFOLD2_BINDCRAFT_TREE_INVENTORY_SHA256,
                        "generated_manifest_sha256": ALPHAFOLD2_BINDCRAFT_MANIFEST_SHA256,
                    },
                    "colabdesign-mpnn-weights-vanilla": {
                        "source_content_digest_sha256": manifests["colabdesign-mpnn-weights-vanilla"]["content"]["digest"],
                        "archive_sha256": COLABDESIGN_ARCHIVE_SHA256,
                        "source_revision": COLABDESIGN_REVISION,
                        "member_prefix": f"ColabDesign-{COLABDESIGN_REVISION}/colabdesign/mpnn/weights/",
                        "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
                        "tree_entry_count": 5,
                        "tree_total_bytes": COLABDESIGN_VANILLA_TREE_BYTES,
                        "tree_inventory_sha256": COLABDESIGN_VANILLA_TREE_INVENTORY_SHA256,
                    },
                    "colabdesign-mpnn-weights-soluble": {
                        "source_content_digest_sha256": manifests["colabdesign-mpnn-weights-soluble"]["content"]["digest"],
                        "archive_sha256": COLABDESIGN_ARCHIVE_SHA256,
                        "source_revision": COLABDESIGN_REVISION,
                        "member_prefix": f"ColabDesign-{COLABDESIGN_REVISION}/colabdesign/mpnn/weights_soluble/",
                        "mount_path": "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
                        "tree_entry_count": 5,
                        "tree_total_bytes": COLABDESIGN_SOLUBLE_TREE_BYTES,
                        "tree_inventory_sha256": COLABDESIGN_SOLUBLE_TREE_INVENTORY_SHA256,
                    },
                },
                "private_artifact": {
                    "artifact_id": pyrosetta_binding["artifact_id"],
                    "source_artifact_id": pyrosetta_binding["source_artifact_id"],
                    "source_plane": "academic-assets",
                    "cache_scope": "tenant-private",
                    "general_shared_cache_allowed": False,
                    "embed_in_image": pyrosetta_binding["embeds_bytes"],
                    "read_only": pyrosetta_binding["read_only"],
                    "source_sub_path": pyrosetta_binding["source_sub_path"],
                    "mount_path": pyrosetta_binding["consumer_path"],
                    "pythonpath": pyrosetta_contract["delivery"]["runtime_consumption"]["pythonpath"],
                    "content_identity_kind": pyrosetta_binding["content_identity_kind"],
                    "tree_manifest_algorithm": pyrosetta_tree["tree_manifest_algorithm"],
                    "tree_manifest_sha256": pyrosetta_tree["tree_manifest_sha256"],
                    "tree_total_bytes": pyrosetta_tree["tree_total_bytes"],
                    "files_installed": pyrosetta_tree["files_installed"],
                    "source_archive_sha256": pyrosetta_binding["source_artifact"]["sha256"],
                    "source_archive_bytes": pyrosetta_binding["source_artifact"]["size_bytes"],
                    "contract": "../academic-assets/contracts/academic-assets.json",
                    "evidence": "../academic-assets/evidence/live-acceptance-state.json",
                },
                "localization_contract": {
                    "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                    "ownership": "primary-artifact-localization-successor",
                    "state": "identity-coordinated-not-integrated",
                },
            },
            "alphafold3": {
                "private_parameters": catalog["private_layouts"]["alphafold3"],
                "public_databases": catalog["reference_layouts"]["alphafold3"],
                "general_cache_parameters_allowed": False,
            },
        },
    }
    output[ROOT / "runtime-integration.json"] = integration
    output[ROOT / "artifact-catalog.json"] = catalog
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failed = False
    for path, value in documents().items():
        rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                print(f"generated artifact metadata is stale: {path.relative_to(ROOT)}", file=sys.stderr)
                failed = True
        else:
            path.write_text(rendered, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
