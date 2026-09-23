# AMBER academic App integration

The operator confirmed academic use and the AMBER agreement on23 September2026.
Official standalone PMEMD26 was acquired and its publisher MD5 verified. No
public AMBER NIM was found; this is a privately built native AMBER App, not an
NVIDIA NIM or an AmberTools substitute. Licensed source/binaries are not in Git.

## Reproducible components

- Source SHA256: `0478ccce892f3525e995e9c85458d552c6060b73dd28acd03c366e61ecf23a14`.
- PMEMD26 engine: CUDA12.8.1, GNU13, native SM89/SM90, SPFP and DPFP.
  Private base digest `744b8d42a846f93118d9288efed67546236cfafd2f7d2377e5c99bc7b9a6dcca`.
- AmberTools26: CPU-only conda-forge package,166 explicit locked packages,
  prefix `/opt/ambertools`. Layer digest
  `c6444becd326ae171a8c9b992fc43e00c052891d6a5a549bbbc5e9f8d49081da`.
- Combined engine digest
  `cea619dcb5f8a8577a17edd7ce0fdd70e6718838f9d5af47d7731ca8388fab48`.
  This is the worker's `ENGINE_ID`; it is distinct from the licensed source hash
  and from the final wrapper image digest.

Build recipes are in `runtime/` and `tools/`. Tool subprocesses select their own
`AMBERHOME`, PATH and library directory; they do not override PMEMD globally.
Only private regional registry images are used for licensed runtime delivery.

## Shared platform path

The generic native-MD adapter handles `amber` through the existing scheduler,
queues, usage attribution, native checkpoint companion and Object Storage
transport. There is no separate tenant or licence authentication mechanism.
Existing App grants control access; scientific inputs remain customer controlled.

Typed steps: `pmemd`, `tleap`, `cpptraj`, `parmed`. Complete native inputs are
uploaded as `amber-input-bundle/v1`; parameters follow
`fs2-serve.nebius.ai/amber-workflow-request/v1`. The intended typed MCP tool is
`submit_amber_workflow`, using `run-workflow`. Preparation/analysis steps need
explicit expected outputs, and dynamics requires exact expected step count.

`build_native_contracts.py --model amber` generates candidate schemas/profile.
`prepare_native_release.py` joins a real successful native receipt to the exact
worker image and complete source recipe, preserves existing App rows and quota,
and creates the normal Helm overlay. A native receipt permits hosted testing;
it does not establish customer readiness. Do not publish a blanket feature claim.

## Evidence and current boundaries

Actual LEaP ff19SB ACE-ALA-NME/OPC preparation, ParmEd topology checks and CPPTRAJ
NetCDF readback passed. The water-box coordinates are preparation evidence, not
an equilibrated system. CPPTRAJ classic NetCDF is supported; this package lacks
HDF5 compression. The first H100 upstream cohort passed13/14 comparisons; the
SPFP sodium-TI temperature tolerance exception is retained, not relaxed. DPFP
for that case passed. Sustained and hosted qualification is recorded separately.

Completed stages commit a closed workspace. An interrupted active stage retries
from its preceding committed stage; arbitrary partial restart files and complete
stochastic state are not promised. Native restart is not a GPU process snapshot.
Persistent CUDA restore and its end-to-end benefit need separate evidence.

One native process/one GPU is the initial target. Coupled MPI/replica exchange,
other GPU architectures, every AmberTools program, and advanced scientific
methods are not qualified just because the source suite supports them. Tool
presence and successful exit are not validation of sampling or convergence.
Customer output quota is unchanged; plan trajectory/checkpoint cadence within it.

The customer skill is maintained in `rene-tech/serverless-ai-cookbook`,
`skills/scientific-ai/amber`. It uses the shared artifact client and never embeds
PMEMD source, binaries, customer credentials or licence assertions for strangers.
