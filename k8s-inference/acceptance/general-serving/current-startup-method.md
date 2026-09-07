# Current Qwen and Cosmos startup measurement

The runner `current-startup-benchmark.py` creates three isolated replicas from a
live model Deployment template. It preserves the image digest, model revision,
arguments, precision, GPU and CPU resources, probes, model PVC, and node/pool
selection. Each repetition has a new pod and new emptyDir runtime caches. The
public Qwen replica and both models' controller policies remain unchanged.

The clone has unique workload/service labels, retains Kueue admission labels,
and is deleted only after checking its recorded Deployment UID. There is no
global page-cache eviction, driver change, GPU mode change, or quota change.

Run once per model, in separate terminals if capacity permits:

```bash
python3 k8s-inference/acceptance/general-serving/current-startup-benchmark.py \
  --kubeconfig /path/to/kubeconfig --context k8s-inference-h100 \
  --model qwen3-8b \
  --source-deployment qwen3-8b-b300-hot-h100-reserved-8x \
  --run-id UNIQUE_RUN_ID --output-dir /private/new-qwen-receipt

python3 k8s-inference/acceptance/general-serving/current-startup-benchmark.py \
  --kubeconfig /path/to/kubeconfig --context k8s-inference-h100 \
  --model cosmos3-nano \
  --source-deployment cosmos3-nano-burst-h100-1x \
  --run-id UNIQUE_RUN_ID --output-dir /private/new-cosmos-receipt
```

The source Deployment is read from the specified cluster at execution time;
its complete template and SHA-256 are retained. A subsequent run may use a newer
production model, so compare template/model/image identities before comparing
results. The historical `b300` string in the Qwen Deployment name does not define
hardware: this test uses the live H100 selector and checks the GPU inside every
measured pod.

| Clock | Start | End |
|---|---|---|
| Process → application ready | Runtime container `startedAt` | Timestamped runtime `Application startup complete` log |
| Process → Kubernetes ready | Runtime container `startedAt` | Pod `Ready` condition transition |
| Pod → Kubernetes ready | Pod creation timestamp | Pod `Ready` condition transition |
| Pod → first validated output | Pod creation timestamp | First direct fixture response received |
| First request latency | Client sends first fixture | Complete response received |

Container and pod timestamps have one-second granularity. Log timestamps are
more precise. Pod readiness includes the configured health probe cadence. The
client waits for observed readiness and port-forward setup before the first
request, so pod-to-first-output also includes that small observation/setup gap.
The first response is validated afterward against the retained fixture. No
public request-to-ready measurement is claimed by this isolated-replica runner.

Both fixtures use the first accepted case from
`catalog/runtime/validators/assets/{qwen3-8b,cosmos3-nano}.json`, with a recorded
file hash and complete non-secret request. Qwen must return the exact expected
text. Cosmos must pass its revision/envelope/base64/MP4/hash validator; when
`ffprobe` is installed, decoding also checks 448×256 and all 25 frames. Cosmos
uses seed 2407, eight steps, guidance 6.0 and 24 fps, unchanged from the fixture.

Private artifacts retain every pod/deployment, timestamped container and init
log, Kubernetes event, node identity, GPU identity, fixture validation and
cleanup result. `receipt.json` is updated during execution. The script refuses
to overwrite an existing receipt. The default is three repetitions; a smaller
explicit repetition count supports supplemental trials, and those must be
combined with matching prior trials to reach three per comparable cohort.
Every failed attempt is retained and stops that cohort.

Report individual runs and median/minimum/maximum only. Three samples do not
support a meaningful p95. Distinguish a newly provisioned node and image pull
from reuse of an existing node/image. Shared model weights remain on the
existing PVC; filesystem page-cache residency is uncontrolled and must never be
described as disk-cold without further evidence.

`current-startup-validate-media.py` provides an independent full MP4 decode
when the client has no `ffprobe`. Run it in a disposable virtual environment
with `av==15.1.0` installed and pass every retained MP4 path. It reports decoded
frame count, dimensions, frame rate, codec, frame diversity, and content hash;
save its JSON output as the media validation receipt.

`current-startup-summarize.py` consumes the Qwen receipt directory, original
Cosmos receipt directory, a supplemental Cosmos trial directory, and that media
validation receipt. It checks zero restarts, successful fixture validation,
completed cleanup, equality of every cloned runtime pod spec to the source,
matching Cosmos model/template contracts across runs, and full decode of every
measured output. It requires at least three cached-image trials per model and
excludes newly provisioned/image-pull trials from those statistics. For the
September 7 run, Cosmos used three initial repetitions plus one supplemental
trial, giving three cached-image repetitions and one new-node sample.
