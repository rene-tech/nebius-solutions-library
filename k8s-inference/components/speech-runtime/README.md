# Nemotron Speech runtime — implementation in progress

This component is **not publicly deployed or customer-qualified**. It establishes the
shared contracts, exact model pins, bounded audio framing and an adapter over
NVIDIA NeMo's cache-aware RNNT pipeline. It does not replace the control plane's
authentication, admission, durable operations, artifact delivery or billing.

Initial task-owned preemptible H100 diagnostics passed for both models; see
[raw evidence and measured limitations](../../acceptance/nemotron-speech-20260916/README.md).
The initial image contains the probe/adapter, not the later stream lifecycle
runner. Neither successful diagnostic is a public/customer acceptance result.

Two Apps are planned: `nemotron-speech-en-0.6b` and
`nemotron-speech-multilingual-0.6b`. Their independent checkpoint revisions and
licenses are defined in `src/fs2_speech/contracts.py`. Application IDs differ
from upstream repository names deliberately; discoverable descriptions must
include the upstream names. No catalog readiness claims have been added.

## Development and direct-runtime diagnostic

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

The Dockerfile uses a digest-pinned PyTorch base, a checksummed NeMo source
archive, and a dependency lock. Re-resolve deliberately, not at startup:

```bash
uv pip compile pyproject.toml --extra nemo --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --no-emit-package nemo-toolkit --output-file requirements-nemo.lock --quiet
```

NeMo itself is installed from the checksummed archive rather than an unhashed
Git dependency in the pip hash-verified installation. Build from a committed
source snapshot; record the pushed image digest before GPU use.

`fs2-speech-probe --model nemotron-speech-en-0.6b` uses task-generated English
speech, one warmup and three repetitions each at file speed and real-time pace.
It retains every result and checks early partials plus a distinctive tail word.
It does **not** qualify language accuracy, public transport, long-file behavior,
mixed traffic, snapshots, scaling or the complete platform. The multilingual
diagnostic with English audio is not multilingual quality qualification.

## Streaming contract foundation

Start options explicitly negotiate mono 16 kHz signed little-endian PCM16.
File codecs will be converted explicitly by the file adapter; PCM wire format
does not imply that browsers already produce it. Odd/empty/oversized audio
messages fail validation. Frame buffering retains at most one model frame
between input messages and preserves the last incomplete/exact-boundary frame.
This currently adds up to one frame of lookahead; it is not a final latency
optimization or an advertised end-to-end latency guarantee.

Each partial **replaces** the text for its segment ID and has a monotonically
increasing revision and session sequence. A final seals that segment. A runtime
that fails to finalize its tail must not be reported as completed. No acoustic
timestamp is inferred from a sequence number. GPU work must complete before
session state is released.

The initial NeMo adapter admits one stream per loaded immutable profile. Language
is per-stream; chunk size, decoding strategy, precision, language-tag retention,
confidence and CUDA-graph configuration are worker settings. A request/profile
mismatch fails instead of mutating a shared model. Multi-profile routing and
measured continuous batching remain implementation work, not delivered features.

## Next integration work

See [implementation and qualification plan](../../docs/nemotron-speech-onboarding-20260916.md).
Public WebSocket/file routes, durable long-file jobs, tenant grants, typed MCP,
admin integration, fair live/batch admission, scalable deployment profiles and
the full customer-path qualification matrix are still open. No unauthenticated
runtime is exposed publicly and no alternate auth/billing system is introduced.
