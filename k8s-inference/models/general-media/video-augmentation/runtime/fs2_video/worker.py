"""Durable, serial preview/batch worker. A completed run may contain rejected clips."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import MAX_TOTAL_BYTES, RESULT_SCHEMA
from .contracts import AugmentationRequest, canonical, recipe_identity
from .media import digest, finalize_audio, inspect_video, validate_alignment
from .paidf import pipeline_config, provider


def publish(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def run(request: AugmentationRequest, workspace: Path, operation_id: str) -> dict:
    models = provider()
    recipe = recipe_identity(
        request.recipe,
        vlm_model=models["vlm"],
        llm_model=models["llm"],
        provider_url=models["url"],
    )
    if (
        request.approved_recipe_sha256
        and request.approved_recipe_sha256 != recipe["sha256"]
    ):
        raise ValueError(
            "approved recipe does not match this exact pipeline/model configuration"
        )
    if len(request.items) > 1 and not request.approved_recipe_sha256:
        raise ValueError("batch requires an approved preview recipe")
    manifest = request.model_dump(by_alias=True)
    frozen = workspace / "request-identity.json"
    if frozen.exists() and frozen.read_bytes() != canonical(manifest):
        raise ValueError("workspace request identity changed")
    publish(frozen, manifest)
    publish(workspace / "recipe.json", recipe)
    sources = {
        item.id: workspace / "inputs" / (item.id + ".mp4") for item in request.items
    }
    if sum(path.stat().st_size for path in sources.values()) > MAX_TOTAL_BYTES:
        raise ValueError("batch exceeds 2 GiB input bound")
    cancelled = False
    child = None

    def stop(_signum, _frame):
        nonlocal cancelled
        cancelled = True
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    results = []
    for item in request.items:
        if cancelled:
            raise SystemExit(143)
        folder = workspace / "outputs" / item.id
        folder.mkdir(parents=True, exist_ok=True)
        checkpoint = folder / "result.json"
        if checkpoint.exists():
            result = json.loads(checkpoint.read_text())
            if (
                result["source_sha256"] != item.sha256
                or result["recipe_sha256"] != recipe["sha256"]
            ):
                raise ValueError("checkpoint identity mismatch")
            if (
                result.get("artifact")
                and digest(workspace / result["artifact"]["path"])
                != result["artifact"]["sha256"]
            ):
                raise ValueError("checkpoint output digest mismatch")
            results.append(result)
            continue
        result = {
            "id": item.id,
            "source_name": item.source_name,
            "source_sha256": item.sha256,
            "recipe_sha256": recipe["sha256"],
            "status": "failed",
        }
        try:
            if (
                sum(value.get("artifact", {}).get("size_bytes", 0) for value in results)
                + 128 * 1024**2
                > MAX_TOTAL_BYTES
            ):
                raise ValueError(
                    "remaining batch output budget cannot safely accommodate another 128 MiB clip"
                )
            source = sources[item.id]
            info = inspect_video(source)
            if info["sha256"] != item.sha256:
                raise ValueError("source digest differs from frozen manifest")
            config = pipeline_config(source, folder, request.recipe, models)
            publish(folder / "config.json", config)
            environment = {
                **os.environ,
                "FS2_VIDEO_OPERATION_ID": operation_id,
                "FS2_VIDEO_CACHE": str(folder / "cache"),
            }
            # Logs stay in the private workspace, never become caller-visible artifacts.
            with (folder / "pipeline.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "fs2_video.bridge",
                        "--config",
                        str(folder / "config.json"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                )
                try:
                    code = child.wait(timeout=7200)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise ValueError("pipeline timed out")
                finally:
                    child = None
            if cancelled:
                raise SystemExit(143)
            metadata_path = folder / "metadata.json"
            if (
                not metadata_path.is_file()
                or metadata_path.stat().st_size > 2 * 1024**2
            ):
                raise ValueError("pipeline did not publish bounded evaluation evidence")
            metadata = json.loads(metadata_path.read_text())
            output_info = inspect_video(folder / "generated.mp4")
            validate_alignment(info, output_info)
            # Do not publish provider requests, URLs or opaque model responses.
            checks = {
                name: {
                    key: metadata.get(name, {}).get(key)
                    for key in ("passed", "score", "threshold", "attempt", "seed_used")
                }
                for name in ("hallucination_check", "attribute_verification")
            }
            passed = code == 0 and all(
                check["passed"] is True for check in checks.values()
            )
            final = folder / "video.mp4"
            from uuid import uuid4

            pending = folder / f"finalizing-{uuid4()}.mp4"
            finalize_audio(
                source,
                folder / "generated.mp4",
                pending,
                preserve=request.recipe.audio == "preserve",
            )
            pending.replace(final)
            final_info = inspect_video(final)
            validate_alignment(info, final_info)
            result.update(
                status="accepted" if passed else "rejected",
                checks=checks,
                prompt=str(metadata.get("prompt", ""))[:8000],
                input=info,
                output=final_info,
                artifact={
                    "path": str(final.relative_to(workspace)),
                    "sha256": final_info["sha256"],
                    "size_bytes": final_info["size_bytes"],
                    "media_type": "video/mp4",
                },
                limitation="Automated motion and sampled-weather checks are not proof of physical fidelity or label validity; human review is required.",
            )
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            result.update(
                error_code="VIDEO_AUGMENTATION_FAILED", error_detail=str(error)[:500]
            )
        usage_path = folder / "provider-usage.jsonl"
        if usage_path.exists() and usage_path.stat().st_size <= 128 * 1024:
            result["provider_usage"] = [
                json.loads(line) for line in usage_path.read_text().splitlines()
            ]
        publish(checkpoint, result)
        # Disposable generated duplicates only; originals and published outputs stay intact.
        (folder / "generated.mp4").unlink(missing_ok=True)
        for cache_file in (folder / "cache").glob("*.mp4"):
            if cache_file.is_file() and not cache_file.is_symlink():
                cache_file.unlink()
        results.append(result)
        publish(
            workspace / "progress.json",
            {"completed": len(results), "total": len(request.items)},
        )
    outcome = {
        "schema": RESULT_SCHEMA,
        "operation_id": operation_id,
        "recipe": recipe,
        "items": results,
        "counts": {
            status: sum(item["status"] == status for item in results)
            for status in ("accepted", "rejected", "failed")
        },
    }
    publish(workspace / "result.json", outcome)
    return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    from uuid import UUID

    UUID(args.operation_id)
    request = AugmentationRequest.model_validate_json(args.request.read_bytes())
    run(request, args.workspace, args.operation_id)


if __name__ == "__main__":
    main()
