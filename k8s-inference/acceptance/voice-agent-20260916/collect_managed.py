"""Save non-secret managed voice resources, actual endpoints and scraped metrics."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

from collect_runtime_evidence import pod_row
from public_smoke import VOICE_MODELS


def main(args):
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "fs2-models",
    ]

    def get(*tail):
        return json.loads(
            subprocess.check_output(command + ["get", *tail, "-o", "json"])
        )

    models = get("modeldeployments", *VOICE_MODELS)["items"]
    deployments = get("deployments", *VOICE_MODELS, "--show-managed-fields")["items"]
    services = get("services", *VOICE_MODELS)["items"]
    pods = get("pods", "-l", "workload.fs2.nebius/owner=voice-agent-20260916")["items"]
    slices = get(
        "endpointslices",
        "-l",
        "kubernetes.io/service-name in (" + ",".join(VOICE_MODELS) + ")",
    )["items"]
    preview = get(
        "deployments",
        "fs2-voice-parakeet-r20260916",
        "fs2-voice-magpie-r20260916",
        "fs2-voice-sortformer-r20260916",
    )["items"]
    events = get("events")["items"]
    autoscalers = {
        kind: [
            {
                "name": item["metadata"]["name"],
                "target": item["spec"]["scaleTargetRef"]["name"],
                "spec": item["spec"],
                "status": item.get("status"),
            }
            for item in get(kind)["items"]
            if item.get("spec", {}).get("scaleTargetRef", {}).get("name")
            in VOICE_MODELS
        ]
        for kind in ("horizontalpodautoscalers", "scaledobjects")
    }
    if args.require_stable:
        for model in models:
            assert model["spec"]["availability"]["minReplicas"] == 1
            assert model["spec"]["availability"]["maxReplicas"] == 2
            assert model["spec"]["cache"]["snapshotPreference"] == "Never"
            assert (
                model["status"]["observedGeneration"] == model["metadata"]["generation"]
            )
            assert model["status"]["phase"] == "Ready", model["metadata"]["name"]
        for deployment in deployments:
            desired = deployment["spec"]["replicas"]
            assert 1 <= desired <= 2
            assert (
                deployment["status"]["observedGeneration"]
                == deployment["metadata"]["generation"]
            )
            assert deployment["status"].get("readyReplicas") == desired
            assert deployment["status"].get("updatedReplicas") == desired
            assert deployment["status"].get("replicas") == desired
            assert not deployment["status"].get("terminatingReplicas", 0)
            assert all(
                entry["manager"] != "fs2-model-controller-replica-handoff"
                or "f:replicas" not in entry.get("fieldsV1", {}).get("f:spec", {})
                for entry in deployment["metadata"].get("managedFields", [])
            )
        for pod in pods:
            assert not pod["metadata"].get("deletionTimestamp")
            assert (
                pod["metadata"]["labels"]["app.kubernetes.io/managed-by"]
                == "fs2-model-controller"
            )
            assert any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod["status"]["conditions"]
            )
        for service in services:
            identity = service["metadata"]["name"]
            model = next(m for m in models if m["metadata"]["name"] == identity)
            assert any(
                o["uid"] == model["metadata"]["uid"] and o.get("controller")
                for o in service["metadata"].get("ownerReferences", [])
            )
            expected = {
                p["metadata"]["name"]
                for p in pods
                if p["metadata"]["labels"].get("app.kubernetes.io/name") == identity
            }
            actual = {
                e["targetRef"]["name"]
                for s in slices
                if s["metadata"]["labels"]["kubernetes.io/service-name"] == identity
                for e in s["endpoints"]
                if e["conditions"].get("ready")
            }
            assert actual == expected and actual, identity
        assert all(p["spec"]["replicas"] == 0 for p in preview)
    receipt = {
        "observed_at": datetime.now(UTC).isoformat(),
        "context": args.context,
        "namespace": "fs2-models",
        "node_loss_tested": False,
        "stable_checks_passed": bool(args.require_stable),
        "autoscalers": autoscalers,
        "models": [
            {
                "name": m["metadata"]["name"],
                "uid": m["metadata"]["uid"],
                "generation": m["metadata"]["generation"],
                "availability": m["spec"]["availability"],
                "snapshot_preference": m["spec"]["cache"]["snapshotPreference"],
                "observed_generation": m.get("status", {}).get("observedGeneration"),
                "phase": m.get("status", {}).get("phase"),
                "replicas": m.get("status", {}).get("replicas"),
            }
            for m in models
        ],
        "deployments": [
            {
                "name": d["metadata"]["name"],
                "uid": d["metadata"]["uid"],
                "strategy": d["spec"]["strategy"],
                "replicas": d["spec"].get("replicas"),
                "replica_field_managers": [
                    entry["manager"]
                    for entry in d["metadata"].get("managedFields", [])
                    if "f:replicas" in entry.get("fieldsV1", {}).get("f:spec", {})
                ],
                "image": d["spec"]["template"]["spec"]["containers"][0]["image"],
                "status": d["status"],
            }
            for d in deployments
        ],
        "services": [
            {
                "name": s["metadata"]["name"],
                "uid": s["metadata"]["uid"],
                "cluster_ip": s["spec"]["clusterIP"],
                "selector": s["spec"]["selector"],
                "owners": s["metadata"].get("ownerReferences"),
            }
            for s in services
        ],
        "endpoints": [
            {
                "service": s["metadata"]["labels"]["kubernetes.io/service-name"],
                "endpoints": [
                    {
                        "pod": e.get("targetRef", {}).get("name"),
                        "conditions": e["conditions"],
                    }
                    for e in s["endpoints"]
                ],
            }
            for s in slices
        ],
        "pods": [pod_row(p) for p in pods],
        "preview_deployments": [
            {"name": p["metadata"]["name"], "replicas": p["spec"]["replicas"]}
            for p in preview
        ],
        "startup_events": [
            {
                "pod": e["involvedObject"]["name"],
                "reason": e["reason"],
                "message": e["message"],
                "at": e.get("lastTimestamp"),
            }
            for e in events
            if any(
                e["involvedObject"]["name"].startswith(m + "-") for m in VOICE_MODELS
            )
            and e["reason"] in {"Scheduled", "Pulled", "Started", "Killing"}
        ],
        "metrics": {},
    }
    with httpx.Client(base_url=args.prometheus, trust_env=False, timeout=15) as client:
        for query in (
            "fs2_voice_ready",
            "fs2_voice_occupied",
            "fs2_voice_requests_total",
            "fs2_voice_audio_seconds_total",
            "fs2_voice_first_output_seconds_count",
        ):
            response = client.get("/api/v1/query", params={"query": query})
            response.raise_for_status()
            receipt["metrics"][query] = response.json()["data"]["result"]
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"models": len(models), "pods": len(pods), "output": args.output}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "prometheus", "output"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--require-stable", action="store_true")
    main(parser.parse_args())
