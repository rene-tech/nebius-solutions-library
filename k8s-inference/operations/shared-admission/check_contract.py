"""Exercise the deployed resolver against an exact candidate scheduling contract.

This checks tenant routing and full-Pod fit, not model output or live capacity.
Run with the control-plane source on PYTHONPATH.
"""

import argparse
import json
from pathlib import Path

from fs2_serve.scientific_batch.models import (
    ScientificBatchPlan,
    ScientificStagePlan,
    StagePlacementClass,
    StageResourceEnvelope,
)
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver


def check(contract):
    resolver = SchedulingContractResolver(contract)
    routes = []
    for service in ("customer-batch", "bulk-backfill", "interactive", "presentation"):
        for tenant, model, expected in [
            ("lynx", "gromacs", "lynx-md"),
            ("lynx", "gromacs-mpi", "lynx-md"),
            ("lynx", "amber", "lynx-md"),
            ("lynx", "namd", "lynx-md"),
            ("lynx", "lammps", "lynx-md"),
            ("whitelab", "scvi-scanvi", "whitelab-single-cell"),
            ("system", "gromacs", "inference-models"),
            ("system", "scvi-scanvi", "inference-models"),
            ("kopra", "bindcraft", "academic-scientific"),
            ("rene", "alphafold3", "academic-scientific"),
        ]:
            actual = resolver._resolve_route(
                service_class=service,
                model_id=model,
                tenant_id=tenant,
                default_local_queue="inference-models",
                desired_local_queue=None,
            )
            assert actual[0] == expected, (tenant, model, service, actual)
            routes.append(
                {
                    "tenant": tenant,
                    "model": model,
                    "service_class": service,
                    "queue": actual[0],
                    "namespace": actual[1],
                }
            )
    shapes = []
    for tenant, model, gib in [
        ("lynx", "gromacs", 16),
        ("whitelab", "scvi-scanvi", 128),
        ("whitelab", "scvi-scanvi", 256),
    ]:
        memory = gib * 1024**3
        resources = StageResourceEnvelope(
            cpu_millis=8000,
            limit_cpu_millis=8000,
            memory_bytes=memory,
            limit_memory_bytes=memory,
            ephemeral_storage_bytes=32 * 1024**3,
            limit_ephemeral_storage_bytes=32 * 1024**3,
        )
        stage = {
            "id": "probe",
            "resource_class": "gpu",
            "admission_mode": "independent-jobs",
            "min_parallelism": 1,
            "max_parallelism": 1,
            "checkpoint_mode": "restart",
            "preemption_mode": "restartable",
        }
        snapshot = resolver.freeze(
            service_class="customer-batch",
            model_id=model,
            tenant_id=tenant,
            profile={"resources": {"gpu_count": 1}, "workload": {"stages": [stage]}},
            plan=ScientificBatchPlan(
                (
                    ScientificStagePlan(
                        stage_id="probe",
                        resources=resources,
                        placement_class=StagePlacementClass.ACCELERATOR,
                    ),
                )
            ),
        )
        pools = snapshot.stages[0].resolved_pool_preference
        expected_first = (
            "l40s-4x"
            if model == "gromacs"
            else ("h100-ondemand-1x" if gib == 128 else "h100-reserved-8x")
        )
        assert pools[0] == expected_first, pools
        if gib == 256:
            assert pools == ("h100-reserved-8x",), pools
        shapes.append(
            {
                "model": model,
                "worker_cpu": 8,
                "worker_memory_gib": gib,
                "eligible_pools": pools,
                "includes_collector_overhead": True,
            }
        )
    return {
        "routing_checks": routes,
        "whole_pod_fit_checks": shapes,
        "passed": True,
        "scope": "resolver and resource-shape checks, not end-to-end model qualification",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = check(json.loads(args.contract.read_text()))
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": True,
                "routes": len(receipt["routing_checks"]),
                "shapes": len(receipt["whole_pod_fit_checks"]),
            }
        )
    )
