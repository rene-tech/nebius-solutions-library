# Recorded LeRobot writer repair

The real two-episode ALOHA request `b46eb725-733c-4543-aab8-91039f11a6bb`
generated both videos, then failed while writing `next.done`: parquet exposed a
scalar boolean although the declared feature shape was `[1]`. The original
failed public operation is retained; successful child inference did not make
the parent workflow successful.

Worker source `1a873c3b8787ac6a485c6f63a12f407d7aef113e` preserves the
declared shape and dtype without changing action/state values. Exact published
image `sha256:dacf151564d8b3387e81ad2bd8fc227dab8b7493c92c1d63758356057f4083c9`
was independently run as UID10001 with a read-only root and no network. It
rewrote the original 128 frames using the two retained H100 child MP4s, reopened
the resulting LeRobot0.6.1 dataset, and preserved all 6,144 non-video values.
Untouched camera streams were re-encoded, not byte-identical; measured maximum
frame mean absolute pixel differences were below 1.67/255. Action alignment
and physical motion fidelity are not established by these checks.

The exact-image receipt is
`/home/tux/secure-handoff/lerobot-recorded-writer-build-20260918-Mi6ZQifj/full-image-writer-receipt.json`,
SHA256 `2897d1da60ac53d1703ce43f3fe5e9dbf3225afa5de34017a6778de151e5818f`.
This is CPU-coordinator plus retained-H100-child evidence, not a fresh complete
public replay. The legacy qualification field name includes `h100`; its scope
is explicitly recorded rather than claiming new GPU execution.

`prepare_promotion.py` validates that receipt, preserves every unrelated
scientific execution row and cluster snapshot record, and produces an
**active/unqualified** image-only successor. It uses actual registry, scheduler,
manifest-renderer, admin-bootstrap and Helm checks. Public completion and
scheduler receipts remain unset until the exact new image is exercised.
Portable source maps do not absorb cluster-specific checkpoint paths.

At preparation, the baseline was deployed Helm168. The private candidate and
rollback contracts are under
`scientific-qualification-20260918/lerobot-writer-promotion168-r2`.
Promotion and original-payload public replay are pending at this checkpoint.
