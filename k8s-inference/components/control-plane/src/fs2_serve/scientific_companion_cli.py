"""Small CLI for scientific workload preparation, localization and collection."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

from .entrypoint import SCIENTIFIC_COMPANION_COMMANDS
from .scientific_batch.companion import (
    WorkloadArtifactHttpClient,
    _contained,
    _write_exclusive,
    collect_and_commit,
    materialize_artifact,
    prepare_workspace,
    verify_runtime_artifacts,
)
from .scientific_batch.models import MaterializationMode
from .scientific_batch.stage_descriptor import MAX_BYTES, RELATIVE_PATH, verified_descriptor


def _client(parser: argparse.ArgumentParser) -> WorkloadArtifactHttpClient:
    api_url = os.environ.get("FS2_SCIENTIFIC_INTERNAL_API_URL")
    capability = os.environ.get("FS2_SCIENTIFIC_WORKLOAD_CAPABILITY")
    if not api_url or not capability:
        parser.error("scientific companion API and capability are required")
    return WorkloadArtifactHttpClient(
        base_url=api_url,
        capability=capability,
        fallback_base_url=os.environ.get("FS2_SCIENTIFIC_INTERNAL_FALLBACK_API_URL"),
    )


def _descriptor(content: bytes | None = None) -> dict[str, Any]:
    path = os.environ.get("FS2_STAGE_DESCRIPTOR_FILE")
    digest = os.environ.get("FS2_STAGE_DESCRIPTOR_SHA256")
    if not path or not digest:
        raise ValueError("stage descriptor identity is required")
    if content is None:
        target = _contained(Path(path))
        if target.is_symlink():
            raise ValueError("stage descriptor must be a private regular file")
        with target.open("rb") as source:
            content = source.read(MAX_BYTES + 1)
    return verified_descriptor(content, digest)


def _materialize(client: WorkloadArtifactHttpClient, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if (
        not args.artifact_id
        or not args.destination
        or not args.mode
        or not args.expected_digest
        or args.expected_size_bytes is None
        or not args.expected_media_type
    ):
        parser.error("scientific materialization identity, destination, and mode are required")
    materialize_artifact(
        client=client,
        artifact_id=UUID(args.artifact_id),
        destination=Path(args.destination),
        mode=MaterializationMode(args.mode),
        compression=args.compression,
        yaml_name=args.yaml_name,
        reuse_prefix=args.reuse_prefix,
        expected_digest=args.expected_digest,
        expected_size_bytes=args.expected_size_bytes,
        expected_media_type=args.expected_media_type,
    )


def main() -> None:
    def terminate(signum: int, _frame: object) -> None:
        # These companion commands are PID 1 in their own containers too.
        # Exit through finally blocks so a cancelled stage's collector cannot
        # retain the GPU Pod until Kubernetes exhausts its termination grace.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    parser = argparse.ArgumentParser(prog="fs2-serve")
    parser.add_argument("command", choices=SCIENTIFIC_COMPANION_COMMANDS)
    parser.add_argument("--logical-artifact-id")
    parser.add_argument("--artifact-id")
    parser.add_argument("--destination")
    parser.add_argument("--mode", choices=tuple(item.value for item in MaterializationMode))
    parser.add_argument("--compression")
    parser.add_argument("--yaml-name")
    parser.add_argument("--reuse-prefix")
    parser.add_argument("--expected-digest")
    parser.add_argument("--expected-size-bytes", type=int)
    parser.add_argument("--expected-media-type")
    parser.add_argument("--commands-json", action="append", default=[])
    parser.add_argument("--collector-id")
    parser.add_argument("--workspace")
    parser.add_argument("--logical-output-id")
    parser.add_argument("--validator-id")
    parser.add_argument("--max-artifacts", type=int)
    parser.add_argument("--max-output-bytes", type=int)
    parser.add_argument("--collection-deadline-seconds", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("FS2_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Artifact handles are presigned URLs. Retain our phase/identity logs,
    # without letting HTTP library INFO messages copy those handles to Loki.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.command == "scientific-prepare-workspace":
        if not args.workspace:
            parser.error("scientific workspace is required")
        runtime_localization_json = os.environ.get("FS2_RUNTIME_ARTIFACTS_JSON")
        stage_invocation_json = os.environ.get("FS2_STAGE_INVOCATION_JSON")
        descriptor_content = None
        if os.environ.get("FS2_STAGE_DESCRIPTOR_FILE"):
            if os.environ["FS2_STAGE_DESCRIPTOR_FILE"] != str(Path(args.workspace) / RELATIVE_PATH):
                parser.error("stage descriptor path differs from the private workspace")
            client = _client(parser)
            try:
                descriptor_content = client.stage_descriptor()
                descriptor = _descriptor(descriptor_content)
                stage_invocation_json = json.dumps(descriptor["invocation"])
                if descriptor["invocation"]["working_directory"] != args.workspace:
                    parser.error("stage descriptor names a different workspace")
            finally:
                client.client.close()
        if not runtime_localization_json or not stage_invocation_json:
            parser.error("scientific runtime localization marker and stage invocation are required")
        prepare_workspace(
            Path(args.workspace),
            runtime_localization_json=runtime_localization_json,
            stage_invocation_json=stage_invocation_json,
        )
        if descriptor_content is not None:
            _write_exclusive(Path(args.workspace) / RELATIVE_PATH, descriptor_content)
        return
    if args.command == "scientific-verify-runtime-artifacts":
        runtime_localization_json = os.environ.get("FS2_RUNTIME_ARTIFACTS_JSON")
        if not runtime_localization_json:
            parser.error("scientific runtime localization marker is required")
        verify_runtime_artifacts(runtime_localization_json=runtime_localization_json)
        return

    client = _client(parser)
    try:
        if args.command == "scientific-materialize":
            _materialize(client, args, parser)
        elif args.command == "scientific-materialize-many":
            if not args.commands_json and os.environ.get("FS2_STAGE_DESCRIPTOR_FILE"):
                entries = [argparse.Namespace(**item) for item in _descriptor()["materializations"]]
            else:
                try:
                    groups = [json.loads(value) for value in args.commands_json]
                except ValueError:
                    parser.error("materialization commands must be JSON")
                if not groups or not all(isinstance(group, list) and group for group in groups):
                    parser.error("materialization commands must be non-empty argument groups")
                commands = [command for group in groups for command in group]
                if not all(
                    isinstance(command, list) and command and all(isinstance(arg, str) for arg in command)
                    for command in commands
                ):
                    parser.error("materialization commands must be a non-empty list of argument lists")
                # Backwards-compatible decoding for already admitted Jobs.
                entries = [parser.parse_args(["scientific-materialize", *command]) for command in commands]

            def materialize_entry(index: int, entry: argparse.Namespace) -> None:
                started = time.monotonic()
                _materialize(client, entry, parser)
                logging.info(
                    "scientific_materialization_complete index=%s total=%s artifact=%s bytes=%s elapsed_seconds=%.6f",
                    index + 1,
                    len(entries),
                    entry.logical_artifact_id,
                    entry.expected_size_bytes,
                    time.monotonic() - started,
                )

            if os.environ.get("FS2_STAGE_DESCRIPTOR_FILE") and len(entries) > 1:
                with ThreadPoolExecutor(max_workers=8, thread_name_prefix="scientific-input") as executor:
                    for start in range(0, len(entries), 128):
                        group = entries[start : start + 128]
                        client.prepare_downloads(tuple(UUID(entry.artifact_id) for entry in group))
                        futures = [
                            executor.submit(materialize_entry, start + index, entry)
                            for index, entry in enumerate(group)
                        ]
                        for future in futures:
                            future.result()
            else:
                for index, entry in enumerate(entries):
                    materialize_entry(index, entry)
        else:
            invocation = os.environ.get("FS2_STAGE_INVOCATION_JSON")
            if os.environ.get("FS2_STAGE_DESCRIPTOR_FILE"):
                invocation = json.dumps(_descriptor()["invocation"])
            if (
                not args.collector_id
                or not args.validator_id
                or not args.workspace
                or not invocation
                or args.max_artifacts is None
                or args.max_output_bytes is None
                or args.collection_deadline_seconds is None
            ):
                parser.error(
                    "scientific collector identity, workspace, invocation, and collection deadline are required"
                )
            # Settings is needed only by collection for the catalog location.
            # Keep the existing setting/default behavior without paying for
            # application configuration validation in every init container.
            from .settings import Settings

            collect_and_commit(
                client=client,
                collector_id=args.collector_id,
                validator_id=args.validator_id,
                invocation_json=invocation,
                workspace=Path(args.workspace),
                catalog_dir=Settings().catalog_dir,
                collection_deadline_seconds=args.collection_deadline_seconds,
                max_artifacts=args.max_artifacts,
                max_output_bytes=args.max_output_bytes,
            )
    finally:
        client.client.close()


if __name__ == "__main__":
    main()
