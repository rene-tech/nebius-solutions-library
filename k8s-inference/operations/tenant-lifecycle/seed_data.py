#!/usr/bin/env python3
"""Publish/verify existing qualified packs; seed with Nebius Data Transfer v1.

No bucket creation, identity changes, unbounded sync, overwrite or delete.
The existing StarterPack validator/installer owns pack semantics and checksums.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from lifecycle import LifecycleError, private_output, read_json


def transfer_spec(project, source, destination, version, digest):
    if not re.fullmatch(r"v[1-9][0-9]*", version) or not re.fullmatch(
        r"[a-f0-9]{64}", digest
    ):
        raise LifecycleError(
            "Require a version and pinned SHA-256 from a qualified starter release"
        )
    for bucket in (source, destination):
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket["bucket_name"]
        ) or not bucket.get("region"):
            raise LifecycleError("Invalid bucket identity or missing region")
    if (source["bucket_name"], source["region"]) == (
        destination["bucket_name"],
        destination["region"],
    ):
        raise LifecycleError("Source and destination must be different buckets")
    prefix = f"examples/{version}/"
    identity = json.dumps(
        [
            project,
            source["bucket_name"],
            source["region"],
            destination["bucket_name"],
            destination["region"],
            version,
            digest,
        ]
    )
    return {
        "metadata": {
            "parent_id": project,
            "name": "fs2-seed-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        },
        "spec": {
            "source": {
                "nebius": {
                    "bucket_name": source["bucket_name"],
                    "region": source["region"],
                },
                "prefix": prefix,
            },
            "destination": {
                "nebius": {
                    "bucket_name": destination["bucket_name"],
                    "region": destination["region"],
                },
                "prefix": prefix,
            },
            "after_one_iteration": {},
            "overwrite_strategy": "NEVER",
            "enable_deletes_in_destination": False,
            "touch_unmanaged": False,
        },
    }


def verify_binding(existing, wanted):
    actual = existing["spec"]
    for side in ("source", "destination"):
        if actual.get(side, {}).get("prefix", "") != wanted["spec"][side]["prefix"]:
            raise LifecycleError("Existing transfer has a different prefix")
        for field in ("bucket_name", "region"):
            if (
                actual.get(side, {}).get("nebius", {}).get(field)
                != wanted["spec"][side]["nebius"][field]
            ):
                raise LifecycleError("Existing transfer belongs to different buckets")
    if (
        "after_one_iteration" not in actual
        or actual.get("overwrite_strategy") != "NEVER"
        or actual.get("enable_deletes_in_destination", False)
        or actual.get("touch_unmanaged", False)
    ):
        raise LifecycleError(
            "Existing transfer does not have the non-destructive one-time configuration"
        )


class TransferCLI:
    def __init__(self, binary, profile):
        self.prefix = [binary, "--profile", profile, "storage", "transfer"]
        result = subprocess.run(
            self.prefix + ["--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise LifecycleError(
                "Nebius CLI lacks storage transfer v1; select a current isolated CLI with --nebius-bin. No v1alpha1 fallback or cloud change was attempted"
            )

    def call(self, command, args):
        result = subprocess.run(
            self.prefix + [command, *args, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode:
            # The CLI may echo credential-bearing requests in errors. Never emit its raw output.
            raise LifecycleError(
                f"Nebius transfer {command} failed; reconcile its stable name before retrying"
            )
        value = json.loads(result.stdout)
        if value.get("error") or value.get("status", {}).get("error"):
            raise LifecycleError(
                f"Nebius transfer {command} returned an operation error"
            )
        return value

    def find(self, project, name):
        result = self.call("list", ["--parent-id", project, "--all"])
        if result.get("next_page_token"):
            raise LifecycleError("Transfer listing is incomplete")
        matches = [x for x in result.get("items", []) if x["metadata"]["name"] == name]
        if len(matches) > 1:
            raise LifecycleError("Ambiguous transfer name; no action taken")
        return matches[0] if matches else None


def start_transfer(cli, wanted, source, destination, timeout):
    project, name = wanted["metadata"]["parent_id"], wanted["metadata"]["name"]
    existing = cli.find(project, name)
    if existing:
        verify_binding(existing, wanted)
    else:
        request = copy.deepcopy(wanted)
        for side, credentials in (("source", source), ("destination", destination)):
            request["spec"][side]["nebius"]["access_key"] = {
                "access_key_id": credentials["access_key_id"],
                "secret_access_key": credentials["secret_access_key"],
            }
        # NamedTemporaryFile creates 0600 files; no keys in argv, logs or retained plans.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="fs2-transfer-", suffix=".json"
        ) as handle:
            json.dump(request, handle)
            handle.flush()
            cli.call("create", ["--file", handle.name])
    deadline = time.monotonic() + timeout
    while True:
        current = cli.find(project, name)
        if current:
            verify_binding(current, wanted)
            status = current.get("status", {})
            state = status.get("state")
            iteration = status.get("last_iteration", {})
            if (
                state in {"FAILED", "FAILING", "DELETING"}
                or status.get("suspension_state") == "SUSPENDED"
            ):
                raise LifecycleError(
                    "Managed transfer failed/suspended; do not hand off this workspace"
                )
            if state == "STOPPED":
                if iteration.get("state") != "COMPLETED":
                    raise LifecycleError(
                        "Transfer stopped without a completed iteration"
                    )
                return {
                    "transfer_id": current["metadata"]["id"],
                    "transfer_name": name,
                    "copy_state": "completed",
                    "verification_state": "required",
                    "last_iteration_state": iteration["state"],
                }
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"Transfer {name} remains pending; resume observation using the same name, do not create another transfer"
            )
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def pack_tools(source):
    if source:
        sys.path.insert(0, str(Path(source).resolve()))
    try:
        import boto3

        module = importlib.import_module("fs2_serve.starter_packs")
    except ImportError:
        raise LifecycleError(
            "Missing boto3/botocore or fs2_serve.starter_packs; use the qualified control-plane environment or --platform-source"
        ) from None
    return boto3, module


def load_pack(args):
    boto3, module = pack_tools(args.platform_source)
    pack = module.StarterPack.load(Path(args.pack_dir))
    if pack.digest != args.manifest_sha256:
        raise LifecycleError("Local qualified pack does not match the pinned manifest")
    return boto3, module, pack


def installer(boto3, module, credentials, quota):
    endpoint = credentials["endpoint"]
    if not endpoint.startswith("https://"):
        raise LifecycleError("Use the HTTPS Object Storage endpoint from the platform")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=credentials["region"],
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
    )
    return module.BucketPackInstaller(
        client, credentials["bucket_name"], quota_bytes=quota
    )


def verify_pack(writer, pack):
    if (
        writer._matches(pack.prefix + "manifest.json", len(pack.manifest), pack.digest)
        is not True
    ):
        raise LifecycleError("Seed manifest is absent or differs; no ready handoff")
    for item in pack.objects:
        if (
            writer._matches(pack.prefix + item.path, item.size_bytes, item.sha256)
            is not True
        ):
            raise LifecycleError(
                "A seeded object is absent/modified/corrupt; customer data was not overwritten"
            )
    return {
        "state": "verified",
        "manifest_sha256": pack.digest,
        "version": pack.version,
        "object_count": len(pack.objects) + 1,
        "total_bytes": pack.total_bytes,
    }


def required_headroom(writer, pack):
    objects = [("manifest.json", len(pack.manifest), pack.digest)]
    objects.extend((obj.path, obj.size_bytes, obj.sha256) for obj in pack.objects)
    needed = 0
    for path, size, checksum in objects:
        match = writer._matches(pack.prefix + path, size, checksum)
        if match is False:
            raise LifecycleError(
                "Destination contains modified example data; preserve it instead of re-seeding this version"
            )
        if match is None:
            needed += size
    return needed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("publish", "verify", "transfer"):
        c = sub.add_parser(name)
        c.add_argument("--pack-dir", required=True)
        c.add_argument("--manifest-sha256", required=True)
        c.add_argument("--platform-source")
        c.add_argument(
            "--credentials",
            required=True,
            help="private bucket-bound destination credentials JSON",
        )
        c.add_argument("--quota-bytes", type=int, default=5_000_000_000)
        c.add_argument(
            "--receipt", help="new private output path for the sanitized result"
        )
        if name != "verify":
            c.add_argument("--apply", action="store_true")
        if name == "transfer":
            c.add_argument("--source-credentials", required=True)
            c.add_argument("--project", required=True)
            c.add_argument("--profile", required=True)
            c.add_argument("--nebius-bin", default="nebius")
            c.add_argument("--wait-seconds", type=float, default=600)
    args = p.parse_args(argv)
    if args.quota_bytes <= 0:
        raise LifecycleError("Quota must be positive")
    if args.receipt and Path(args.receipt).exists():
        raise LifecycleError("Receipt already exists; retain it and choose a new path")
    boto3, module, pack = load_pack(args)
    credentials = read_json(args.credentials)
    if args.command == "publish":
        result = {
            "action": "publish_qualified_source",
            "bucket": credentials["bucket_name"],
            "prefix": pack.prefix,
            "manifest_sha256": pack.digest,
            "bytes": pack.total_bytes,
            "overwrite": False,
            "apply": args.apply,
        }
        if args.apply:
            result = installer(boto3, module, credentials, args.quota_bytes).install(
                pack
            )
    elif args.command == "verify":
        result = verify_pack(
            installer(boto3, module, credentials, args.quota_bytes), pack
        )
    else:
        source = read_json(args.source_credentials)
        spec = transfer_spec(
            args.project, source, credentials, pack.version, pack.digest
        )
        result = {
            "action": "seed_from_canonical_bucket",
            "transfer": spec,
            "apply": args.apply,
            "manifest_sha256": pack.digest,
            "verification": "full SHA-256 after completed iteration",
        }
        if args.apply:
            if args.wait_seconds < 0:
                raise LifecycleError("Wait duration must be nonnegative")
            # Confirm CLI compatibility before any source/destination work.
            cli = TransferCLI(args.nebius_bin, args.profile)
            verify_pack(installer(boto3, module, source, args.quota_bytes), pack)
            target = installer(boto3, module, credentials, args.quota_bytes)
            # Replays need headroom only for missing objects. Provider quota remains authoritative.
            if target._usage() + required_headroom(target, pack) > args.quota_bytes:
                raise LifecycleError(
                    "Insufficient quota headroom for conservative seeding check"
                )
            result = start_transfer(cli, spec, source, credentials, args.wait_seconds)
            result["verification"] = verify_pack(target, pack)
            result["verification_state"] = "verified"
    if args.receipt:
        private_output(args.receipt, result)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2))
    except Exception as error:
        # SDK/CLI exceptions can contain credentials. Print only curated errors.
        message = (
            str(error) if isinstance(error, LifecycleError) else type(error).__name__
        )
        print(f"Seed command did not complete: {message}", file=sys.stderr)
        sys.exit(1)
