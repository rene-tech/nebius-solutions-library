# Workspace file integration — 2026-10-08

The workbench fix lives in `rene-tech/serverless-ai-cookbook`,
`templates/hcls-librechat/docs/workspace-files-20261008.md` (full evidence and
rollback). Runtime source `36b9fa9c2ac64102f22efc01eb6ed635fad3ac71`; image index
`sha256:752a860b307e85e69b8ad3b5983ac158f78ad5121bb8dbf4f5ec6e89a30e0c1b`.

The existing `register_workbench_release.py` compare-and-test helper added
`workspace-20261008-r3` to the API release menu. This directory retains its exact
desired Helm values fragment; reconcile it with future intentional releases,
not an old full Helm configuration. All three API replicas became ready. No
backend image, public route, GPU placement, key, limit or job was changed.

At the owner's request WhiteLab's current Serverless UI was patched in place,
not replaced: endpoint `aiendpoint-e00axdvs33j7ycdqt1`, VM and URL unchanged.
Three chats / 12 messages, 16 App grants and storage identity were preserved.
The cloud image remains catalog-R2; R3 patch/rollback records persist under
`/data/workbench-hotfixes/workspace-20261008-r3`. For a later Serverless VM
recreation use R3 or reapply the retained patch; do not call it a cloud-image
upgrade. No other customer endpoint was touched.

File UI acceptance passed with two internal QA cohorts and read-only customer
file selection. A separate full-agent model timeout remains documented in the
workbench release note and Task Deck; do not infer all-agent/model readiness.
