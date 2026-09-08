# Live source query check — September 8, 2026

At 11:56:02 UTC the new app observation service was exercised read-only through
Kubernetes service proxies on `k8s-inference-h100`, for DiffDock's exact
ModelDeployment selector. No live application or model setting was changed.
This is a source-query check, **not** post-deployment browser acceptance.

The preceding hour returned all seven chart families successfully:

| Observation | Average | Maximum |
|---|---:|---:|
| Concurrent logical requests | 0 | 0 |
| CPU cores | 0.00087705 | 0.00101970 |
| RAM working-set bytes | 1,168,047,006 | 1,168,289,792 |
| Running containers | 1 | 1 |
| Ready containers | 1 | 1 |
| GPU utilization, percent per allocated device | 0 | 0 |
| GPU memory, bytes per allocated device | 3,449,815,040 | 3,449,815,040 |

Each family returned 241 points per series. Loki returned five requested log
lines; the current inventory returned one model container. Log contents and
credentials are deliberately excluded. GPU observations used the Pod's recorded
device UUID and allocation interval, not the DCGM exporter's own Pod labels.

The repeatable query checker is
[`check_observability_sources.py`](check_observability_sources.py). The app's
public API and browser still need to be checked after the integrated release.
