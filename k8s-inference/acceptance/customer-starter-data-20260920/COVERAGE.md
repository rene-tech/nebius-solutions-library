# Tested example coverage

These measurements include gateway queueing and runtime startup where applicable. They are one observed run per recipe, not a latency guarantee or isolated GPU benchmark. An occupied cluster can take longer. Cold-start values are reported only when the platform measured them; missing is not zero.

| Category / case | Model | Input formats | Observed accepted-to-terminal time |
| --- | --- | --- | --- |
| sequence-search/1ubq | msa-search-pdb70 | text/x-fasta | 4.6s |
| structure/1ubq | openfold2 | chemical/x-pdb, text/x-fasta | 4.0s |
| protein-design/sequence-1ubq | proteinmpnn | chemical/x-pdb, text/x-fasta | 1.6s |
| sequence-search/1crn | msa-search-pdb70 | text/x-fasta | 4.8s |
| structure/1crn | openfold2 | chemical/x-pdb, text/x-fasta | 3.6s |
| protein-design/sequence-1crn | proteinmpnn | chemical/x-pdb, text/x-fasta | 1.4s |
| sequence-search/1pga | msa-search-pdb70 | text/x-fasta | 3.3s |
| structure/1pga | openfold2 | chemical/x-pdb, text/x-fasta | 4.8s |
| protein-design/sequence-1pga | proteinmpnn | chemical/x-pdb, text/x-fasta | 1.6s |
| sequence-search/1l2y | msa-search-pdb70 | text/x-fasta | 3.3s |
| structure/1l2y | openfold2 | chemical/x-pdb, text/x-fasta | 2.5s |
| protein-design/sequence-1l2y | proteinmpnn | chemical/x-pdb, text/x-fasta | 2.1s |
| sequence-search/1vii | msa-search-pdb70 | text/x-fasta | 3.5s |
| structure/1vii | openfold2 | chemical/x-pdb, text/x-fasta | 2.9s |
| sequence-search/1bdd | msa-search-pdb70 | text/x-fasta | 3.1s |
| sequence-search/2gb1 | msa-search-pdb70 | text/x-fasta | 1.0s |
| sequence-search/1bpi | msa-search-pdb70 | text/x-fasta | 4.4s |
| sequence-search/1lyz | msa-search-pdb70 | text/x-fasta | 3.5s |
| sequence-search/2trx | msa-search-pdb70 | text/x-fasta | 4.6s |
| small-molecule/aspirin | molmim | chemical/x-daylight-smiles | 4.5s |
| small-molecule/aspirin | genmol | chemical/x-daylight-smiles | 1.4s |
| small-molecule/caffeine | molmim | chemical/x-daylight-smiles | 8.3s |
| small-molecule/caffeine | genmol | chemical/x-daylight-smiles | 2.0s |
| small-molecule/paracetamol | molmim | chemical/x-daylight-smiles | 3.9s |
| small-molecule/ibuprofen | molmim | chemical/x-daylight-smiles | 4.8s |
| small-molecule/vanillin | molmim | chemical/x-daylight-smiles | 5.9s |
| small-molecule/salicylic-acid | molmim | chemical/x-daylight-smiles | 3.1s |
| small-molecule/nicotinamide | molmim | chemical/x-daylight-smiles | 4.0s |
| small-molecule/menthol | molmim | chemical/x-daylight-smiles | 3.8s |
| small-molecule/theobromine | molmim | chemical/x-daylight-smiles | 3.1s |
| small-molecule/catechol | molmim | chemical/x-daylight-smiles | 4.1s |
| genomics/balanced | evo2-40b | text/x-fasta | 5.8s |
| genomics/at-rich | evo2-40b | text/x-fasta | 5.6s |
| genomics/gc-rich | evo2-40b | text/x-fasta | 3.1s |
| genomics/short-context | evo2-40b | text/x-fasta | 5.6s |
| genomics/longer-context | evo2-40b | text/x-fasta | 2.9s |
| genomics/gc-transition | evo2-40b | text/x-fasta | 6.1s |
| genomics/low-complexity | evo2-40b | text/x-fasta | 5.3s |
| genomics/alternating | evo2-40b | text/x-fasta | 5.9s |
| genomics/homopolymer-flank | evo2-40b | text/x-fasta | 5.4s |
| genomics/mixed-composition | evo2-40b | text/x-fasta | 5.3s |
| imaging/chest-1 | nv-reason-cxr-3b | application/json, image/png | 4.8s |
| imaging/chest-2 | nv-reason-cxr-3b | application/json, image/png | 3.7s |
| imaging/chest-3 | nv-reason-cxr-3b | application/json, image/png | 4.3s |
| imaging/chest-4 | nv-reason-cxr-3b | application/json, image/png | 5.4s |
| imaging/chest-5 | nv-reason-cxr-3b | application/json, image/png | 8.3s |
| imaging/single-ellipsoid | nv-segment-ct | application/gzip | 0.9s |
| imaging/paired-ellipsoids | nv-segment-ct | application/gzip | 1.5s |
| imaging/elongated-ellipsoid | nv-segment-ct | application/gzip | 2.4s |
| imaging/sparse-cells | cellpose-cpsam-v2 | image/png | 2.8s |
| imaging/sparse-cells | sam2-1-hiera-large | image/png | 2.6s |
| imaging/crowded-cells | cellpose-cpsam-v2 | image/png | 1.4s |
| imaging/crowded-cells | sam2-1-hiera-large | image/png | 2.7s |
| single-cell/balanced | scvi-scanvi | application/x-hdf5 | 4.8s |
| single-cell/batch-shift | scvi-scanvi | application/x-hdf5 | 4.1s |
| single-cell/small-study | scvi-scanvi | application/x-hdf5 | 2.2s |
| single-cell/more-cells | scvi-scanvi | application/x-hdf5 | 5.3s |
| single-cell/sparse-counts | scvi-scanvi | application/x-hdf5 | 1.7s |
| single-cell/rare-population | scvi-scanvi | application/x-hdf5 | 1.7s |
| single-cell/three-batches | scvi-scanvi | application/x-hdf5 | 1.9s |
| single-cell/wider-gene-panel | scvi-scanvi | application/x-hdf5 | 2.1s |
| single-cell/partial-labels | scvi-scanvi | application/x-hdf5 | 4.5s |
| single-cell/imbalanced-labels | scvi-scanvi | application/x-hdf5 | 3.7s |
| age-prediction/altumage-center | altumage | application/json | 3.1s |
| age-prediction/altumage-higher-methylation | altumage | application/json | 1.8s |
| age-prediction/altumage-lower-methylation | altumage | application/json | 1.5s |
| age-prediction/altumage-sparse-perturbation | altumage | application/json | 1.7s |
| age-prediction/altumage-mixed-perturbation | altumage | application/json | 2.7s |
| age-prediction/phenoage-baseline-30 | phenoage | application/json | 1.1s |
| age-prediction/phenoage-baseline-60 | phenoage | application/json | 1.5s |
| age-prediction/phenoage-inflammation | phenoage | application/json | 2.1s |
| age-prediction/phenoage-lower-albumin | phenoage | application/json | 1.0s |
| age-prediction/phenoage-changed-cell-fractions | phenoage | application/json | 2.0s |
| speech/en-gastroenteritis | nemotron-speech-multilingual-0-6b | audio/flac | 26.2s |
| speech/en-gastroenteritis | nemotron-speech-en-0-6b | audio/flac | 26.1s |
| speech/en-gastroenteritis | parakeet-realtime-eou-120m-v1 | audio/flac | 145.1s |
| speech/en-gastroenteritis | diar-streaming-sortformer-4spk-v2-1 | audio/flac | 33.0s |
| speech/en-eczema | nemotron-speech-multilingual-0-6b | audio/flac | 33.0s |
| speech/en-eczema | nemotron-speech-en-0-6b | audio/flac | 31.0s |
| speech/en-eczema | parakeet-realtime-eou-120m-v1 | audio/flac | 180.2s |
| speech/en-eczema | diar-streaming-sortformer-4spk-v2-1 | audio/flac | 40.6s |
| speech/de-herzrasen | nemotron-speech-multilingual-0-6b | audio/flac | 23.7s |
| speech/de-infekt | nemotron-speech-multilingual-0-6b | audio/flac | 41.0s |
| speech/de-polyarthritis | nemotron-speech-multilingual-0-6b | audio/flac | 24.7s |
| speech/en-appointment | magpie-tts-multilingual-357m | text/plain | 4.6s |
| speech/de-appointment | magpie-tts-multilingual-357m | text/plain | 6.6s |
| speech/en-lab-workflow | magpie-tts-multilingual-357m | text/plain | 5.8s |
| speech/de-interview | magpie-tts-multilingual-357m | text/plain | 4.2s |
| speech/en-accessibility | magpie-tts-multilingual-357m | text/plain | 6.7s |
| general-ai/explain-fasta | qwen3-8b | text/plain | 121.5s |
| general-ai/summarize-status | qwen3-8b | text/plain | 1.9s |
| general-ai/extract-labels | qwen3-8b | text/plain | 2.6s |
| general-ai/translate-workflow | qwen3-8b | text/plain | 119.7s |
| general-ai/compare-units | qwen3-8b | text/plain | 5.6s |
| general-ai/glassware | sdxl | text/plain | 33.5s |
| general-ai/helix-art | sdxl | text/plain | 3.3s |
| general-ai/robotics-room | sdxl | text/plain | 3.5s |
| general-ai/forest | sdxl | text/plain | 5.5s |
| general-ai/geometric-poster | sdxl | text/plain | 3.2s |
| structure/1bdd | esmfold2 | application/json, chemical/x-pdb, text/x-fasta | 203.2s |
| structure/1bdd | boltz2 | application/json, chemical/x-pdb, text/x-fasta | 30.1s |
| structure/2gb1 | esmfold2-fast | application/json, chemical/x-pdb, text/x-fasta | 93.2s |
| structure/1bpi | protenix-v2 | application/json, chemical/x-pdb, text/x-fasta | 145.9s |
| structure/1lyz | alphafold3 | application/json, chemical/x-pdb, text/x-fasta | 190.8s |
| structure/1lyz | diffdock | application/json, chemical/x-pdb, text/x-fasta | 4.4s |
| structure/2trx | openfold3-openbind | application/json, chemical/x-pdb, text/x-fasta | 253.0s |
| structure/2trx | openfold3 | application/json, chemical/x-pdb, text/x-fasta | 10.7s |
| protein-design/boltzgen | boltzgen | application/gzip, application/json | 1480.4s |
| protein-design/mosaic | mosaic | application/json, text/x-fasta | 427.2s |
| protein-design/proteina-complexa | proteina-complexa | application/json, application/x-tar | 414.6s |
| protein-design/rfdiffusion | rfdiffusion | application/json, text/plain | 369.4s |
| protein-design/rfdiffusion-96 | rfdiffusion | application/json, text/plain | 105.5s |
| protein-design/bindcraft | bindcraft | application/json, chemical/x-pdb | 1106.3s |
| physical-ai-robotics/pick-and-place | cosmos3-nano | text/plain | 308.5s |
| physical-ai-robotics/conveyor | cosmos3-nano | text/plain | 4.5s |
| physical-ai-robotics/mobile-robot | cosmos3-nano | text/plain | 4.5s |
| physical-ai-robotics/pipette-robot | cosmos3-nano | text/plain | 3.0s |
| physical-ai-robotics/gripper | cosmos3-nano | text/plain | 3.3s |
| physical-ai-robotics/warm-lighting | cosmos3-nano | image/png | 3.4s |
| physical-ai-robotics/cool-lighting | cosmos3-nano | image/png | 4.1s |
| physical-ai-robotics/moving-block | cosmos3-nano | video/mp4 | 6.1s |
| physical-ai-robotics/moving-disc | cosmos3-nano | video/mp4 | 3.2s |
| physical-ai-robotics/lerobot-lighting | cosmos3-lerobot-augmentation | application/json, application/x-tar | 34.4s |
