#!/usr/bin/env python3
"""Measure each cold-start mechanism against conventional on live H100.

The arms come from ``mechanism_arms``, which renders them by calling the
production ``fs2_serve.fast_start_mechanisms`` adapters, so a measured
improvement is attributable to the shipped mechanism rather than to a benchmark
fixture.  This runner only sequences attempts, times them, and writes receipts.

Clock discipline, because the numbers are the deliverable:

* A cold attempt is timed from the ``PodScheduled`` condition, which is when
  compatible accelerator capacity is available to this Pod, to the first
  validated semantic response.  Both ends are cluster-side clocks.  The
  condition timestamp has one-second granularity and every receipt says so.
* A promotion attempt is timed inside the cluster from the prober's own clock,
  and the trigger is dispatched after the prober has started waiting, so the
  dispatch is counted against the mechanism.  The reported activation is
  therefore conservative.
* A failed or timed-out attempt is recorded as a failure with a null duration.
  It is never dropped, because a startup class is a reliability claim as well
  as a latency percentile.

Nothing here qualifies a level.  ``fast_start.py`` does that, and only from 20
comparable failure-free samples at p95 for the exact tuple.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from mechanism_arms import (
    ARMS,
    PROMOTION_ARMS,
    ArmError,
    load_contract,
    render_arm,
    storage_contract_digest,
    target,
)

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/fast-start-mechanism-attempt/v1"
SUMMARY_SCHEMA = "fs2-serve.nebius.ai/fast-start-mechanism-comparison/v1"
PROBER_NAME = "fsm-prober"
CONTROL_ARM = "conventional"
# Matches fast_start.MINIMUM_QUALIFYING_SAMPLES and QUALIFYING_PERCENTILE. The
# runner never applies them; it only reports whether they are satisfied.
MINIMUM_QUALIFYING_SAMPLES = 20
QUALIFYING_PERCENTILE = 0.95

PROBE_SCRIPT = r"""
import json, sys, time, urllib.error, urllib.request

url, body, timeout, trigger = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
payload = body.encode("utf-8")
print(json.dumps({"event": "waiting", "epoch": time.time()}), flush=True)
if trigger:
    try:
        urllib.request.urlopen(
            urllib.request.Request(trigger, data=b"{}", headers={"content-type": "application/json"}),
            timeout=30,
        ).read()
    except Exception as error:  # the wake may race the first probe; keep polling
        print(json.dumps({"event": "trigger_error", "detail": type(error).__name__}), flush=True)
deadline = time.time() + timeout
attempts = 0
while time.time() < deadline:
    attempts += 1
    try:
        request = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            document = json.loads(response.read())
        content = document["choices"][0]["message"]["content"]
        if isinstance(content, str) and content.strip():
            print(
                json.dumps(
                    {
                        "event": "ready",
                        "epoch": time.time(),
                        "attempts": attempts,
                        "characters": len(content),
                        "sample": content.strip()[:64],
                    }
                ),
                flush=True,
            )
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.25)
print(json.dumps({"event": "timeout", "epoch": time.time(), "attempts": attempts}), flush=True)
sys.exit(1)
"""

PHASE_PATTERNS = {
    "weight_load_seconds": re.compile(r"Loading weights took ([0-9.]+) seconds"),
    "model_load_seconds": re.compile(r"Model loading took [0-9.]+ GiB memory and ([0-9.]+) seconds"),
    "compile_seconds": re.compile(r"torch\.compile took ([0-9.]+) s in total"),
    "graph_capture_seconds": re.compile(r"Graph capturing finished in ([0-9]+) secs"),
    "engine_init_seconds": re.compile(r"init engine \([^)]*\) took ([0-9.]+) s"),
}


class CampaignError(RuntimeError):
    """A live campaign step failed in a way that must not be papered over."""


class Cluster:
    """A very small ``kubectl`` wrapper.

    The repository's other live harnesses shell out to ``kubectl`` as well; a
    Kubernetes client dependency would buy nothing here and would have to be
    added to the control-plane package for one benchmark.
    """

    def __init__(self, *, kubeconfig: Path, context: str, namespace: str) -> None:
        self.base = [
            "kubectl",
            f"--kubeconfig={kubeconfig}",
            f"--context={context}",
            f"--namespace={namespace}",
        ]

    def run(self, *arguments: str, stdin: str | None = None, check: bool = True) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*self.base, *arguments],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if check and completed.returncode != 0:
            raise CampaignError(f"kubectl {' '.join(arguments)} failed: {completed.stderr.strip()[:400]}")
        return completed.stdout

    def apply(self, manifest: dict[str, Any]) -> None:
        self.run("apply", "-f", "-", stdin=json.dumps(manifest))

    def delete(self, kind: str, name: str) -> None:
        self.run("delete", kind, name, "--ignore-not-found", "--wait=false", check=False)

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        output = self.run("get", kind, name, "-o", "json", check=False)
        if not output.strip():
            return None
        parsed: dict[str, Any] = json.loads(output)
        return parsed

    def logs(self, pod: str, container: str) -> str:
        return self.run("logs", pod, "-c", container, "--tail=400", check=False)

    def exec_stream(self, pod: str, script: str, arguments: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [*self.base, "exec", pod, "--", "python3", "-c", script, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def _iso(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def _condition(pod: dict[str, Any], kind: str) -> str | None:
    for item in pod.get("status", {}).get("conditions", []):
        if item.get("type") == kind and item.get("status") == "True":
            transition = item.get("lastTransitionTime")
            return str(transition) if transition else None
    return None


def _parse_phases(logs: str) -> dict[str, float]:
    phases: dict[str, float] = {}
    for name, pattern in PHASE_PATTERNS.items():
        found = pattern.search(logs)
        if found is not None:
            phases[name] = float(found.group(1))
    return phases


class Campaign:
    def __init__(
        self,
        *,
        cluster: Cluster,
        spec: dict[str, Any],
        campaign_id: str,
        output_dir: Path,
        timeout_seconds: float,
    ) -> None:
        self._cluster = cluster
        self._spec = spec
        self._campaign_id = campaign_id
        self._output = output_dir
        self._timeout = timeout_seconds
        self._output.mkdir(parents=True, exist_ok=True)
        self._parked: dict[str, dict[str, Any]] = {}

    # -- setup ---------------------------------------------------------------

    def ensure_prober(self) -> None:
        """A long-lived in-cluster client so timings use one cluster clock."""

        existing = self._cluster.get("pod", PROBER_NAME)
        if existing is not None and existing.get("status", {}).get("phase") == "Running":
            return
        if existing is not None:
            self._cluster.delete("pod", PROBER_NAME)
            self._await(lambda: self._cluster.get("pod", PROBER_NAME) is None, "prober teardown", 120)
        self._cluster.apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": PROBER_NAME,
                    "namespace": self._spec["namespace"],
                    "labels": {"app.kubernetes.io/managed-by": "fs2-fast-start-mechanism-campaign"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/hostname": self._spec["node_name"]},
                    "tolerations": [
                        {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
                    ],
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True},
                    "containers": [
                        {
                            "name": "prober",
                            "image": self._spec["runtime_image"],
                            "command": ["sleep", "infinity"],
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            }
        )
        self._await(
            lambda: (self._cluster.get("pod", PROBER_NAME) or {}).get("status", {}).get("phase") == "Running",
            "prober readiness",
            600,
        )

    def ensure_holder(self, arm: str) -> dict[str, Any] | None:
        rendered = render_arm(self._spec, arm=arm, attempt=0, campaign_id=self._campaign_id)
        if not rendered["holders"]:
            return None
        for manifest in rendered["holders"]:
            self._cluster.apply(manifest)
        deployment = rendered["holders"][-1]["metadata"]["name"]
        self._await(
            lambda: (self._cluster.get("deployment", deployment) or {}).get("status", {}).get("readyReplicas", 0) >= 1,
            f"{arm} residency holder",
            1800,
        )
        pods = json.loads(
            self._cluster.run(
                "get",
                "pods",
                "-l",
                ",".join(f"{key}={value}" for key, value in rendered["holder_selector"].items()),
                "-o",
                "json",
            )
        )
        receipt = None
        if pods["items"]:
            receipt = self._holder_receipt(pods["items"][0]["metadata"]["name"])
        return {"deployment": deployment, "receipt": receipt}

    def _holder_receipt(self, pod: str) -> dict[str, Any] | None:
        path = f"{self._spec['residency_receipt_mount']}/{self._spec['model_ref']}/receipt.json"
        output = self._cluster.run("exec", pod, "--", "cat", path, check=False)
        try:
            parsed: dict[str, Any] = json.loads(output)
        except ValueError:
            return None
        return parsed

    # -- attempts ------------------------------------------------------------

    def _await(self, predicate: Any, what: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(2.0)
        raise CampaignError(f"timed out waiting for {what}")

    def _semantic_url(self, host: str) -> str:
        return f"http://{host}:{self._spec['service_port']}{self._spec['semantic_request']['path']}"

    def _probe(self, host: str, trigger: str = "") -> subprocess.Popen[str]:
        body = json.dumps(self._spec["semantic_request"]["body"], separators=(",", ":"))
        return self._cluster.exec_stream(
            PROBER_NAME,
            PROBE_SCRIPT,
            [self._semantic_url(host), body, str(self._timeout), trigger],
        )

    @staticmethod
    def _read_events(process: subprocess.Popen[str], queue: Queue[dict[str, Any]]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                queue.put(json.loads(line))
            except ValueError:
                continue

    def cold_attempt(self, arm: str, attempt: int, *, warmup: bool = False) -> dict[str, Any]:
        """Create the arm, time capacity-available to semantic ready, tear down."""

        rendered = render_arm(self._spec, arm=arm, attempt=attempt, campaign_id=self._campaign_id)
        name = rendered["name"]
        self._cluster.apply(rendered["service"])
        self._cluster.apply(rendered["pod"])
        try:
            self._await(lambda: _condition(self._cluster.get("pod", name) or {}, "PodScheduled"), "scheduling", 900)
            pod = self._cluster.get("pod", name) or {}
            created_at = _iso(pod["metadata"]["creationTimestamp"])
            scheduled_raw = _condition(pod, "PodScheduled")
            assert scheduled_raw is not None
            scheduled_at = _iso(scheduled_raw)
            service_host = f"{name}.{self._spec['namespace']}.svc.cluster.local"
            process = self._probe(service_host)
            queue: Queue[dict[str, Any]] = Queue()
            reader = threading.Thread(target=self._read_events, args=(process, queue), daemon=True)
            reader.start()
            outcome = self._collect(queue, process)
            logs = self._cluster.logs(name, self._spec["runtime_container_name"])
            ready_at = outcome.get("epoch")
            succeeded = outcome.get("event") == "ready"
            return self._receipt(
                rendered=rendered,
                attempt=attempt,
                succeeded=succeeded,
                warmup=warmup,
                capacity_wait_seconds=round(scheduled_at - created_at, 3),
                model_start_seconds=round(ready_at - scheduled_at, 3) if succeeded and ready_at else None,
                end_to_end_seconds=round(ready_at - created_at, 3) if succeeded and ready_at else None,
                clock="pod-scheduled-to-semantic-ready",
                phases=_parse_phases(logs),
                outcome=outcome,
            )
        finally:
            self._cluster.delete("pod", name)
            self._cluster.delete("service", name)

    def park(self, arm: str) -> dict[str, Any]:
        """Bring up the replica a promotion arm activates, and hold it."""

        if arm in self._parked:
            return self._parked[arm]
        rendered = render_arm(self._spec, arm=arm, attempt=0, campaign_id=self._campaign_id)
        name = rendered["name"]
        self._cluster.apply(rendered["service"])
        self._cluster.apply(rendered["pod"])
        # A gated standby never reports Ready, so wait on the engine's own
        # startup probe rather than Pod readiness.
        self._await(
            lambda: self._container_started(name),
            f"{arm} standby engine",
            2400,
        )
        self._parked[arm] = rendered
        return rendered

    def _container_started(self, name: str) -> bool:
        pod = self._cluster.get("pod", name) or {}
        for status in pod.get("status", {}).get("containerStatuses", []):
            if status.get("name") == self._spec["runtime_container_name"]:
                if status.get("started") and status.get("ready"):
                    return True
                # A gated Pod reports started once its startup probe passes.
                if status.get("started") and pod.get("status", {}).get("podIP"):
                    return self._engine_healthy(pod["status"]["podIP"])
        return False

    def _engine_healthy(self, host: str) -> bool:
        script = (
            "import sys,urllib.request\n"
            "sys.exit(0 if urllib.request.urlopen(sys.argv[1], timeout=5).status == 200 else 1)\n"
        )
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*self._cluster.base, "exec", PROBER_NAME, "--", "python3", "-c", script,
             f"http://{host}:{self._spec['service_port']}/health"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.returncode == 0

    def promotion_attempt(self, arm: str, attempt: int) -> dict[str, Any]:
        """Time activation of an already-parked replica from one cluster clock."""

        rendered = self.park(arm)
        name = rendered["name"]
        pod = self._cluster.get("pod", name) or {}
        host = pod.get("status", {}).get("podIP")
        if not host:
            raise CampaignError(f"{arm} parked replica has no address")
        trigger = ""
        if arm == "host-memory-residency-sleep-offload":
            self._sleep_engine(host)
            trigger = f"http://{host}:{self._spec['service_port']}/wake_up"
        process = self._probe(host, trigger=trigger)
        queue: Queue[dict[str, Any]] = Queue()
        reader = threading.Thread(target=self._read_events, args=(process, queue), daemon=True)
        reader.start()
        try:
            waiting = queue.get(timeout=120)
        except Empty:
            process.kill()
            raise CampaignError("the prober did not start waiting") from None
        if waiting.get("event") != "waiting":
            process.kill()
            raise CampaignError(f"unexpected prober event {waiting.get('event')}")
        if arm == "gpu-resident":
            self._promote(name, rendered["readiness_gate"])
        outcome = self._collect(queue, process)
        succeeded = outcome.get("event") == "ready"
        ready_at = outcome.get("epoch")
        if arm == "gpu-resident":
            self._demote(name, rendered["readiness_gate"])
        return self._receipt(
            rendered=rendered,
            attempt=attempt,
            succeeded=succeeded,
            capacity_wait_seconds=0.0,
            model_start_seconds=round(ready_at - waiting["epoch"], 3) if succeeded and ready_at else None,
            end_to_end_seconds=round(ready_at - waiting["epoch"], 3) if succeeded and ready_at else None,
            clock="prober-trigger-to-semantic-ready",
            phases={},
            outcome=outcome,
        )

    def _sleep_engine(self, host: str) -> None:
        """Put the engine to sleep so its weights live in host RAM."""

        script = (
            "import sys,urllib.request\n"
            "urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=b''), timeout=120).read()\n"
        )
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*self._cluster.base, "exec", PROBER_NAME, "--", "python3", "-c", script,
             f"http://{host}:{self._spec['service_port']}/sleep?level=1"],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        time.sleep(2.0)

    def _promote(self, name: str, gate: str) -> None:
        self._patch_gate(name, gate, "True")

    def _demote(self, name: str, gate: str) -> None:
        self._patch_gate(name, gate, "False")

    def _patch_gate(self, name: str, gate: str, value: str) -> None:
        patch = {
            "status": {
                "conditions": [
                    {
                        "type": gate,
                        "status": value,
                        "reason": "FastStartMechanismCampaign",
                        "lastTransitionTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                ]
            }
        }
        self._cluster.run(
            "patch", "pod", name, "--subresource=status", "--type=merge", "-p", json.dumps(patch)
        )

    def _collect(self, queue: Queue[dict[str, Any]], process: subprocess.Popen[str]) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout + 120
        while time.monotonic() < deadline:
            try:
                event = queue.get(timeout=5)
            except Empty:
                if process.poll() is not None:
                    break
                continue
            if event.get("event") in ("ready", "timeout"):
                return event
        process.kill()
        return {"event": "timeout", "epoch": None}

    def _receipt(
        self,
        *,
        rendered: dict[str, Any],
        attempt: int,
        succeeded: bool,
        warmup: bool = False,
        capacity_wait_seconds: float | None,
        model_start_seconds: float | None,
        end_to_end_seconds: float | None,
        clock: str,
        phases: dict[str, float],
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "campaign_id": self._campaign_id,
            "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "model_ref": self._spec["model_ref"],
            "arm": rendered["arm"],
            "mechanism": rendered["mechanism"],
            "attempt": attempt,
            "succeeded": succeeded,
            # A warm-up populates the mechanism's retained state and is excluded
            # from the cohort. A retained compile cache has to be written once
            # before it can be read, and measuring that first write as if it
            # were the steady state would understate the mechanism.
            "warmup": warmup,
            "measurement_basis": "CapacityAvailableToSemanticReady",
            "clock": clock,
            "clock_quantisation_seconds": 1.0 if clock.startswith("pod-scheduled") else 0.25,
            "capacity_wait_seconds": capacity_wait_seconds,
            "model_start_seconds": model_start_seconds,
            "end_to_end_seconds": end_to_end_seconds,
            "runtime_phases": phases,
            "mechanism_config_digest": rendered["mechanism_config_digest"],
            "declaration_config_digest": rendered["config_digest"],
            "residency_mode": rendered["residency_mode"],
            "reserved_host_memory_bytes": rendered["reserved_host_memory_bytes"],
            "reserved_accelerators": rendered["reserved_accelerators"],
            "capacity_preheld": rendered["promotion"],
            "tuple": {
                "pool_ref": self._spec["pool_ref"],
                "node_name": self._spec["node_name"],
                "accelerator_class": self._spec["accelerator_class"],
                "capacity_type": self._spec["capacity_type"],
                "startup_scenario": self._spec["startup_scenario"],
                "runtime_image": self._spec["runtime_image"],
                "payload_digest": self._spec["payload_digest"],
                "compile_cache_abi": self._spec["compile_cache_abi"],
                "storage_contract_digest": storage_contract_digest(self._spec),
            },
            "page_cache_state": "held-resident" if rendered["mechanism"] == "host-memory-residency" else "uncontrolled",
            "prober_outcome": outcome,
        }
        prefix = "warmup" if warmup else "attempt"
        path = self._output / f"{prefix}-{rendered['arm']}-{attempt:03d}.json"
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return receipt


def summarise(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the per-mechanism comparison against conventional."""

    by_arm: dict[str, list[dict[str, Any]]] = {}
    for receipt in receipts:
        if receipt.get("warmup"):
            continue
        by_arm.setdefault(receipt["arm"], []).append(receipt)

    def statistics(cohort: list[dict[str, Any]]) -> dict[str, Any]:
        durations: list[float | None] = [
            item["model_start_seconds"] if item["succeeded"] else None for item in cohort
        ]
        ordered = sorted(value for value in durations if value is not None)
        failed = sum(1 for value in durations if value is None)

        def rank(fraction: float) -> float | None:
            if not durations:
                return None
            position = max(1, math.ceil(fraction * len(durations)))
            return ordered[position - 1] if position <= len(ordered) else None

        return {
            "samples": len(durations),
            "failed": failed,
            "p50_seconds": rank(0.5),
            "p95_seconds": rank(QUALIFYING_PERCENTILE),
            "minimum_seconds": ordered[0] if ordered else None,
            "maximum_seconds": ordered[-1] if ordered else None,
        }

    arms = {arm: statistics(cohort) for arm, cohort in sorted(by_arm.items())}
    control = arms.get(CONTROL_ARM, {})
    control_p95 = control.get("p95_seconds")
    comparison = {}
    for arm, value in arms.items():
        p95 = value["p95_seconds"]
        saving = None
        factor = None
        if control_p95 and p95 is not None:
            saving = round(control_p95 - p95, 3)
            factor = round(control_p95 / p95, 2) if p95 else None
        cohort = by_arm[arm]
        comparison[arm] = {
            **value,
            "mechanism": cohort[0]["mechanism"],
            "capacity_preheld": cohort[0]["capacity_preheld"],
            "reserved_host_memory_bytes": cohort[0]["reserved_host_memory_bytes"],
            "reserved_accelerators": cohort[0]["reserved_accelerators"],
            "p95_saving_seconds_vs_conventional": saving,
            "p95_speedup_vs_conventional": factor,
            # The runner reports whether the rule is satisfied. It never
            # applies it: fast_start.py alone decides a level.
            "meets_minimum_samples": value["samples"] >= MINIMUM_QUALIFYING_SAMPLES,
            "failure_free": value["failed"] == 0,
            "qualifies_a_level": False,
            "qualification_owner": "fs2_serve.fast_start.evaluate_fast_start",
        }
    return {
        "schema": SUMMARY_SCHEMA,
        "minimum_qualifying_samples": MINIMUM_QUALIFYING_SAMPLES,
        "qualifying_percentile": QUALIFYING_PERCENTILE,
        "control_arm": CONTROL_ARM,
        "arms": comparison,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--target", default="qwen3-8b")
    parser.add_argument("--node", required=True, help="the node this campaign runs every arm on")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--control-samples", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--warmup-attempts",
        type=int,
        default=1,
        help="excluded attempts per cold arm that populate its retained state",
    )
    parser.add_argument("--teardown", action="store_true", help="remove holders and parked replicas and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    spec = target(load_contract(), arguments.target)
    spec["node_name"] = arguments.node
    spec.setdefault("residency_receipt_mount", "/residency")
    cluster = Cluster(kubeconfig=arguments.kubeconfig, context=arguments.context, namespace=spec["namespace"])
    campaign = Campaign(
        cluster=cluster,
        spec=spec,
        campaign_id=arguments.campaign_id,
        output_dir=arguments.output_dir,
        timeout_seconds=arguments.timeout_seconds,
    )
    if arguments.teardown:
        for arm in ARMS:
            rendered = render_arm(spec, arm=arm, attempt=0, campaign_id=arguments.campaign_id)
            cluster.delete("pod", rendered["name"])
            cluster.delete("service", rendered["name"])
            for manifest in rendered["holders"]:
                cluster.delete(manifest["kind"].lower(), manifest["metadata"]["name"])
        cluster.delete("pod", PROBER_NAME)
        sys.stdout.write("teardown requested; task-owned campaign objects removed\n")
        return 0

    campaign.ensure_prober()
    receipts: list[dict[str, Any]] = []
    control_samples = arguments.control_samples if arguments.control_samples is not None else arguments.samples
    candidates = [arm for arm in arguments.arms if arm != CONTROL_ARM]
    for arm in candidates:
        if arm == "host-memory-residency":
            campaign.ensure_holder(arm)

    cold_arms = [arm for arm in arguments.arms if arm not in PROMOTION_ARMS]
    for round_index in range(arguments.warmup_attempts):
        for arm in cold_arms:
            try:
                warmed = campaign.cold_attempt(arm, round_index, warmup=True)
            except (CampaignError, ArmError) as error:
                sys.stderr.write(f"warm-up {arm}/{round_index} failed: {error}\n")
                continue
            receipts.append(warmed)
            sys.stdout.write(f"{arm} warm-up {round_index}: {warmed['model_start_seconds']}s (excluded)\n")
            sys.stdout.flush()

    # Strict control/candidate alternation, so drift in the shared filesystem or
    # the node cannot favour one arm over another.
    schedule: list[tuple[str, int]] = []
    control_index = 0
    for index in range(max(arguments.samples, control_samples)):
        if CONTROL_ARM in arguments.arms and control_index < control_samples:
            schedule.append((CONTROL_ARM, control_index))
            control_index += 1
        for arm in candidates:
            if index < arguments.samples:
                schedule.append((arm, index))

    for arm, attempt in schedule:
        started = time.monotonic()
        try:
            if arm in PROMOTION_ARMS:
                receipt = campaign.promotion_attempt(arm, attempt)
            else:
                receipt = campaign.cold_attempt(arm, attempt)
        except (CampaignError, ArmError) as error:
            sys.stderr.write(f"attempt {arm}/{attempt} failed: {error}\n")
            continue
        receipts.append(receipt)
        sys.stdout.write(
            f"{arm} attempt {attempt}: "
            f"{'ready in ' + str(receipt['model_start_seconds']) + 's' if receipt['succeeded'] else 'FAILED'}"
            f" (wall {round(time.monotonic() - started, 1)}s)\n"
        )
        sys.stdout.flush()

    summary = summarise(receipts)
    (arguments.output_dir / "comparison.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
