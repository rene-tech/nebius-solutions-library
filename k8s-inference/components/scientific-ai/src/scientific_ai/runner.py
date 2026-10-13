"""One private molecular-dynamics execution. Called by S2, not a serving process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
from pathlib import Path

import httpx
from pydantic import Field

from .broker import BrokerObjects
from .contracts import Contract, GromacsRun, Owner, RuntimeProfile, StorageGrant
from .delivery import retained_workspace
from .gromacs import DeliveryPending, GromacsApplication
from .lifecycle import FileLifecycle
from .native_md import NativeMDApplication, NativeMDRun
from .storage import Artifacts


class PrivateRun(Contract):
    owner: Owner
    run: GromacsRun | NativeMDRun
    profile: RuntimeProfile
    broker_url: str
    storage_token: str = Field(repr=False)


def main(*, finish_delivery: bool = False) -> int:
    work = Path("/work")
    manifest = Path(os.environ.get("PRIVATE_INPUT_DIR", "/var/run/serverless2/private")) / "run.json"
    try:
        binding = PrivateRun.model_validate_json(manifest.read_bytes())
        lifecycle = FileLifecycle(work)
        attempt = lifecycle.discover(
            pod_name=socket.gethostname(),
            owner=binding.owner,
            profile=binding.profile,
        )
        if finish_delivery != (attempt.attempt_role == "finalize"):
            raise PermissionError("entrypoint differs from the authenticated attempt role")
        workspace = work / ("scientific-" + attempt.attempt_id)
        if finish_delivery:
            workspace = retained_workspace(
                work,
                attempt=attempt,
                run=binding.run,
                profile=binding.profile,
                compute_pod=os.environ["FINALIZE_OF"],
                compute_epoch=int(os.environ["FINALIZE_EPOCH"]),
            )
        # Neither request arguments nor environment can select another image,
        # executable, Kubernetes identity, or shared cache.
        with httpx.Client(timeout=httpx.Timeout(120, connect=10, read=30), follow_redirects=False) as http:
            objects = BrokerObjects(
                http=http,
                url=binding.broker_url,
                token=binding.storage_token,
                attempt_id=attempt.attempt_id,
                identity_token_file=Path("/var/run/secrets/workload-identity/token"),
            )
            native = isinstance(binding.run, NativeMDRun)
            application = NativeMDApplication if native else GromacsApplication
            options = {} if native else {"gmx": "/opt/gromacs/bin/gmx"}
            app = application(
                run=binding.run,
                attempt=attempt,
                grant=StorageGrant(
                    owner=binding.owner,
                    root_operation_id=attempt.root_operation_id,
                    grant_ref="request-scoped-broker",
                ),
                profile=binding.profile,
                artifacts=Artifacts(objects),
                lifecycle=lifecycle,
                workspace=workspace,
                **options,
            )
            signal.signal(signal.SIGTERM, app.worker.stop)
            signal.signal(signal.SIGINT, app.worker.stop)
            # Portable resume: S2 passes the durable reference. A fresh attempt
            # never consumes an unacknowledged old workspace or customer cache.
            if finish_delivery:
                result = app.finish_delivery()
            else:
                checkpoint = (
                    lifecycle.checkpoint_reference(attempt) or os.environ.get("CHECKPOINT_REF") or None
                )
                result = app.execute(checkpoint)
            print(
                json.dumps(
                    {
                        "event": "scientific-compute-ended",
                        "attempt_id": attempt.attempt_id,
                        "compute": result["compute"],
                        "export": result["export"],
                    }
                ),
                flush=True,
            )
            return 0 if result["compute"] == "succeeded" else 1
    except DeliveryPending:
        print('{"event":"scientific-delivery-pending","scratch_retained":true}', flush=True)
        return 75
    except Exception as error:
        # SDK exceptions can contain signed URLs/credentials or private input.
        # Return only the exception class to shared logs.
        print(
            json.dumps({"event": "scientific-attempt-failed", "error_type": type(error).__name__}), flush=True
        )
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finish-delivery", action="store_true")
    sys.exit(main(finish_delivery=parser.parse_args().finish_delivery))
