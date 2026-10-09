# Judge calibration, 16 September 2026

Selected fixed judge: `google/gemma-3-27b-it` (Gemma family). No Gemma-family
clinicians are admitted. Qwen-derived clinicians are present, so Qwen judges were
excluded before calibration. Hermes-4-405B (Llama family) was the alternative.

Both candidates returned valid five-axis judgments for all 48 eligible published
human-annotated conversations: 96 successful live judgments, no retries, no
invented/default scores. The original source contains 60 rows. Twelve rows were
excluded conservatively because their patient names occur in the judge's
in-context examples. Names were grouped across records and ordered by SHA-256
of `mindeval-calibration-v1:{name}`. The first six name groups formed the 24-row
selection set; seven remaining groups formed the 24-row validation set. Shared
first names may represent different profiles; grouping is intentionally
conservative and does not assume they identify the same patient.

The selection rule was minimum mean absolute error across all five axes, among
candidates with 100% valid selection judgments. No prompt tuning, score offsets
or candidate selection used the validation metrics. Exact model metadata,
prompt hashes, human means, predictions and usage are in adjacent JSON files.

| Candidate | Selection MAE | Validation MAE | Validation signed bias | Validation RMSE |
| --- | ---: | ---: | ---: | ---: |
| Gemma 27B | 0.43924 | 0.45694 | +0.11250 | 0.57925 |
| Hermes 405B | 0.46285 | 0.57674 | +0.34479 | 0.71053 |

Scores use the original 1–6 scale. Gemma's validation axis biases are +0.0729
clinical accuracy, +0.0590 ethics, +0.3594 assessment, -0.3142 alliance, and +0.3854
AI communication. The overall error obscures these meaningful directional
differences. Model ranking reliability is not established: exact-profile
matched comparisons are sparse, so the adjacent pairwise diagnostic must not be
treated as a ranking-validation result.

Human mapping follows the ordering of the 2+2+2+2+1 sub-axes described in
[paper Appendix C](https://arxiv.org/html/2511.18491v1#A3): fields (1,2), (3,4),
(6,7), (8,9), (10). Field numbering is not explicitly mapped in the source
repository; this is a documented ordering assumption. Out-of-range zero ratings
are excluded as missing. Each sub-axis is averaged across valid raters, then
paired sub-axes are averaged. Undocumented `criterion11` is excluded. This is a
research calibration with a material annotation-schema assumption, not clinical
validation. Family exclusion reduces self-preference risk but does not establish
absence of training-data, provider or patient-model bias.

The original upstream rubric requests plain five-score output while its examples
use `<output>...</output>`. An initial diagnostic run exposed this formatting
variation; a strict parser correction accepts exactly that envelope and still
requires all five scores. Initial failures remain in
`../20260916-parser-diagnostic/`. The final 96 judgments used the corrected parser.

Gemma consumed 1,092,196 prompt + 2,759 completion tokens; mean end-to-end request
latency was 5,974.9 ms including mean queue wait 580.2 ms. Hermes consumed 1,013,374
prompt + 2,780 completion tokens; mean latency was 77,092.5 ms including 74,815.2 ms
queue wait under its discovered 200,000 TPM budget. These are shared Token Factory
endpoint measurements, not dedicated GPU throughput. Physical GPU model and
preemptibility are not exposed by the public catalog. No GPU resource was created.
