#!/usr/bin/env python3
"""Run every committed scientific acceptance input with bounded parallelism.

Each model is delegated to ``run_acceptance.py`` so uploads, submission,
polling, semantic validation, and receipt redaction use the exact same public
HTTPS path as an individual customer run.  The bearer token stays in its
environment variable: it is never placed in argv, output, or a receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

AGGREGATE_SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-aggregate-receipt/v1"
MODEL_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-acceptance-receipt/v1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_PARALLEL = 32
MODEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHILD_ERROR_RE = re.compile(r"^scientific acceptance failed: ([a-z][a-z0-9_]*)$")
FORBIDDEN_KEY_RE = re.compile(
    r"(?:token|password|secret|credential|cookie|authorization|signed_url|storage_key)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?:bearer\s+|x-amz-(?:algorithm|credential|signature|security-token)|"
    r"aws_access_key_id|set-cookie\s*:|authorization\s*:)",
    re.IGNORECASE,
)

EXPECTED_PRIMARY = frozenset(
    {"bindcraft", "boltzgen", "mosaic", "proteina-complexa", "rfdiffusion"}
)
EXPECTED_SECONDARY = frozenset(
    {
        "alphafold3",
        "esmfold2",
        "esmfold2-fast",
        "openfold3-openbind",
        "protenix-v2",
    }
)

InputKind = Literal["primary-activation-fragment", "secondary-public-acceptance"]


class FleetAcceptanceError(RuntimeError):
    """One stable, non-secret orchestration failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FleetInput:
    model_id: str
    kind: InputKind
    path: Path
    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class FleetConfig:
    endpoint: str
    repository_root: Path
    receipt_root: Path
    run_id: str
    token_environment: str = "FS2_INFERENCE_TOKEN"
    max_parallel: int = 4
    timeout_seconds: float = 7200.0
    poll_seconds: float = 5.0
    request_timeout_seconds: float = 60.0
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    input: FleetInput
    status: Literal["succeeded", "failed"]
    receipt: dict[str, Any] | None = None
    receipt_sha256: str | None = None
    receipt_size_bytes: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class FleetRun:
    aggregate: dict[str, Any]
    aggregate_path: Path
    succeeded: int
    failed: int


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FleetAcceptanceError(code)
    return value


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + suffix
        ).encode()
    except (TypeError, ValueError) as error:
        raise FleetAcceptanceError("aggregate_not_canonicalizable") from error


def _assert_redacted(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if FORBIDDEN_KEY_RE.search(str(key)):
                raise FleetAcceptanceError("receipt_redaction_failed")
            _assert_redacted(item)
    elif isinstance(value, list):
        for item in value:
            _assert_redacted(item)
    elif isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        raise FleetAcceptanceError("receipt_redaction_failed")


def _load_json(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise FleetAcceptanceError(code) from error
    if len(raw) > MAX_JSON_BYTES:
        raise FleetAcceptanceError(code)
    try:
        return _object(json.loads(raw), code), raw
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise FleetAcceptanceError(code) from error


def _discover_group(
    repository_root: Path,
    *,
    pattern: str,
    expected: frozenset[str],
    kind: InputKind,
) -> list[FleetInput]:
    discovered: dict[str, FleetInput] = {}
    for path in sorted(repository_root.glob(pattern)):
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(repository_root).as_posix()
        except (OSError, ValueError) as error:
            raise FleetAcceptanceError("acceptance_input_path_invalid") from error
        document, raw = _load_json(resolved, "acceptance_input_invalid")
        model_id = document.get("model_id")
        if not isinstance(model_id, str) or MODEL_RE.fullmatch(model_id) is None:
            raise FleetAcceptanceError("acceptance_input_model_invalid")
        if model_id not in expected:
            continue
        fixtures = document.get("public_fixtures")
        if not isinstance(fixtures, dict) or not isinstance(
            fixtures.get("request"), str
        ):
            raise FleetAcceptanceError("acceptance_input_fixtures_invalid")
        if model_id in discovered:
            raise FleetAcceptanceError("acceptance_input_duplicate")
        discovered[model_id] = FleetInput(
            model_id=model_id,
            kind=kind,
            path=resolved,
            relative_path=relative,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    if set(discovered) != expected:
        raise FleetAcceptanceError("acceptance_input_set_incomplete")
    return list(discovered.values())


def discover_inputs(repository_root: Path) -> tuple[FleetInput, ...]:
    """Discover the five primary and five secondary model-owned records."""

    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise FleetAcceptanceError("repository_root_invalid") from error
    if not root.is_dir():
        raise FleetAcceptanceError("repository_root_invalid")
    primary = _discover_group(
        root,
        pattern="models/cancer-immunotherapy/**/activation/fragment.json",
        expected=EXPECTED_PRIMARY,
        kind="primary-activation-fragment",
    )
    secondary = _discover_group(
        root,
        pattern="models/structure/batch-adapters/*/activation/public-acceptance.json",
        expected=EXPECTED_SECONDARY,
        kind="secondary-public-acceptance",
    )
    combined = sorted((*primary, *secondary), key=lambda item: item.model_id)
    if len(combined) != len(EXPECTED_PRIMARY | EXPECTED_SECONDARY):
        raise FleetAcceptanceError("acceptance_input_set_invalid")
    return tuple(combined)


def _endpoint_identity(endpoint: str) -> dict[str, object]:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise FleetAcceptanceError("endpoint_invalid")
    return {"host": parsed.netloc, "tls": parsed.scheme == "https"}


def _validate_config(
    config: FleetConfig,
) -> tuple[Path, Path, dict[str, object]]:
    if SAFE_ID_RE.fullmatch(config.run_id) is None:
        raise FleetAcceptanceError("run_id_invalid")
    if ENVIRONMENT_RE.fullmatch(config.token_environment) is None:
        raise FleetAcceptanceError("token_environment_invalid")
    token = os.environ.get(config.token_environment)
    if token is None:
        raise FleetAcceptanceError("token_environment_missing")
    if not token or any(character.isspace() for character in token):
        raise FleetAcceptanceError("token_invalid")
    if not 1 <= config.max_parallel <= MAX_PARALLEL:
        raise FleetAcceptanceError("max_parallel_invalid")
    if config.timeout_seconds < 0 or config.poll_seconds <= 0:
        raise FleetAcceptanceError("poll_configuration_invalid")
    if not 0 < config.request_timeout_seconds <= 600:
        raise FleetAcceptanceError("request_timeout_invalid")
    try:
        repository_root = config.repository_root.resolve(strict=True)
    except OSError as error:
        raise FleetAcceptanceError("repository_root_invalid") from error
    if not repository_root.is_dir():
        raise FleetAcceptanceError("repository_root_invalid")
    try:
        receipt_root = config.receipt_root.resolve()
        receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_directory = receipt_root / config.run_id
        if run_directory.is_symlink():
            raise FleetAcceptanceError("receipt_directory_invalid")
        run_directory.mkdir(exist_ok=True, mode=0o700)
        if not run_directory.is_dir():
            raise FleetAcceptanceError("receipt_directory_invalid")
    except OSError as error:
        raise FleetAcceptanceError("receipt_directory_invalid") from error
    return repository_root, run_directory, _endpoint_identity(config.endpoint)


def _child_run_id(run_id: str, model_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{model_id}".encode()).hexdigest()
    return f"fleet-{digest[:40]}"


def _child_error(stderr: bytes, returncode: int) -> str:
    if len(stderr) <= 512:
        try:
            message = stderr.decode("utf-8").strip()
        except UnicodeDecodeError:
            message = ""
        match = CHILD_ERROR_RE.fullmatch(message)
        if match is not None:
            return match.group(1)
    return f"acceptance_child_exit_{returncode}"


def _read_model_receipt(
    path: Path, *, model_id: str, endpoint: dict[str, object]
) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FleetAcceptanceError("model_receipt_missing") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise FleetAcceptanceError("model_receipt_permissions_invalid")
    receipt, raw = _load_json(path, "model_receipt_invalid")
    if receipt.get("schema") != MODEL_RECEIPT_SCHEMA:
        raise FleetAcceptanceError("model_receipt_schema_invalid")
    model = _object(receipt.get("model"), "model_receipt_identity_invalid")
    if model.get("model_id") != model_id or receipt.get("endpoint") != endpoint:
        raise FleetAcceptanceError("model_receipt_identity_invalid")
    for field in (
        "operation_identity",
        "terminal_state",
        "timestamps",
        "cold_start",
        "execution_identity",
        "queue",
        "artifact_digests",
    ):
        _object(receipt.get(field), "model_receipt_incomplete")
    if not isinstance(receipt.get("attempts"), list):
        raise FleetAcceptanceError("model_receipt_incomplete")
    _assert_redacted(receipt)
    return receipt, raw


def _promote_receipt(temporary: Path, final: Path, *, overwrite: bool) -> None:
    try:
        if overwrite:
            os.replace(temporary, final)
            return
        os.link(temporary, final)
        temporary.unlink()
    except FileExistsError as error:
        raise FleetAcceptanceError("receipt_exists") from error
    except OSError as error:
        raise FleetAcceptanceError("receipt_promotion_failed") from error


def _invoke_one(
    config: FleetConfig,
    entry: FleetInput,
    *,
    repository_root: Path,
    run_directory: Path,
    endpoint: dict[str, object],
) -> ModelOutcome:
    final_receipt = run_directory / f"{entry.model_id}.json"
    temporary_receipt = run_directory / f".{entry.model_id}.{uuid4().hex}.pending"
    script = Path(__file__).resolve().with_name("run_acceptance.py")
    command = [
        sys.executable,
        str(script),
        "--endpoint",
        config.endpoint,
        "--token-env",
        config.token_environment,
        "--repository-root",
        str(repository_root),
        "--activation-fragment",
        entry.relative_path,
        "--receipt",
        str(temporary_receipt),
        "--run-id",
        _child_run_id(config.run_id, entry.model_id),
        "--timeout-seconds",
        str(config.timeout_seconds),
        "--poll-seconds",
        str(config.poll_seconds),
        "--request-timeout-seconds",
        str(config.request_timeout_seconds),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - argv is fully separated and model paths are discovered locally.
            command,
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            return ModelOutcome(
                input=entry,
                status="failed",
                error_code=_child_error(completed.stderr, completed.returncode),
            )
        receipt, raw = _read_model_receipt(
            temporary_receipt, model_id=entry.model_id, endpoint=endpoint
        )
        _promote_receipt(temporary_receipt, final_receipt, overwrite=config.overwrite)
        return ModelOutcome(
            input=entry,
            status="succeeded",
            receipt=receipt,
            receipt_sha256=hashlib.sha256(raw).hexdigest(),
            receipt_size_bytes=len(raw),
        )
    except FleetAcceptanceError as error:
        return ModelOutcome(input=entry, status="failed", error_code=error.code)
    except OSError:
        return ModelOutcome(
            input=entry, status="failed", error_code="acceptance_child_start_failed"
        )
    finally:
        try:
            temporary_receipt.unlink(missing_ok=True)
        except OSError:
            pass


def _gpu_attribution(receipt: dict[str, Any]) -> dict[str, Any]:
    """Copy exact API attribution when present; never estimate missing values."""

    for field in (
        "gpu_occupied_idle",
        "gpu_accounting",
        "lifecycle_accounting",
        "resource_accounting",
    ):
        value = receipt.get(field)
        if isinstance(value, dict) and any(
            "occupied" in key or "idle" in key for key in value
        ):
            return {"available": True, "source_field": field, "value": value}
    operation_accounting = receipt.get("operation_accounting")
    if isinstance(operation_accounting, dict) and any(
        field in operation_accounting
        for field in (
            "scheduler_occupied_gpu_seconds",
            "device_allocated_gpu_seconds",
            "active_gpu_seconds",
            "occupied_idle_gpu_seconds",
        )
    ):
        return {
            "available": True,
            "source_field": "operation_accounting",
            "value": operation_accounting,
        }
    return {"available": False, "source_field": None, "value": None}


def _success_entry(outcome: ModelOutcome) -> dict[str, Any]:
    assert outcome.receipt is not None
    receipt = outcome.receipt
    cold_start = _object(receipt["cold_start"], "model_receipt_incomplete")
    return {
        "model_id": outcome.input.model_id,
        "input": {
            "kind": outcome.input.kind,
            "path": outcome.input.relative_path,
            "sha256": outcome.input.sha256,
        },
        "status": "succeeded",
        "receipt": {
            "path": f"{outcome.input.model_id}.json",
            "sha256": outcome.receipt_sha256,
            "size_bytes": outcome.receipt_size_bytes,
        },
        "operation_identity": receipt["operation_identity"],
        "terminal_state": receipt["terminal_state"],
        "execution_identity": receipt["execution_identity"],
        "api_measurements": {
            "cold_start": cold_start,
            "runtime": {
                "runtime_identity": cold_start.get("runtime"),
                "timestamps": receipt["timestamps"],
                "attempts": receipt["attempts"],
            },
            "queue": receipt["queue"],
            "gpu_occupied_idle": _gpu_attribution(receipt),
        },
    }


def _failure_entry(outcome: ModelOutcome) -> dict[str, Any]:
    return {
        "model_id": outcome.input.model_id,
        "input": {
            "kind": outcome.input.kind,
            "path": outcome.input.relative_path,
            "sha256": outcome.input.sha256,
        },
        "status": "failed",
        "error_code": outcome.error_code or "acceptance_child_failed",
        "api_measurements": None,
    }


def _write_atomic(path: Path, body: bytes, *, overwrite: bool) -> None:
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
            temporary = None
        else:
            os.link(temporary, path)
            temporary.unlink()
            temporary = None
    except FileExistsError as error:
        raise FleetAcceptanceError("receipt_exists") from error
    except OSError as error:
        raise FleetAcceptanceError("aggregate_receipt_write_failed") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def run_fleet(config: FleetConfig) -> FleetRun:
    """Execute the discovered fleet and always record a complete outcome table."""

    repository_root, run_directory, endpoint = _validate_config(config)
    inputs = discover_inputs(repository_root)
    aggregate_path = run_directory / "aggregate.json"
    final_paths = [run_directory / f"{entry.model_id}.json" for entry in inputs]
    if not config.overwrite and any(
        os.path.lexists(path) for path in (aggregate_path, *final_paths)
    ):
        raise FleetAcceptanceError("receipt_exists")

    outcomes: list[ModelOutcome] = []
    futures: dict[Future[ModelOutcome], FleetInput] = {}
    with ThreadPoolExecutor(
        max_workers=min(config.max_parallel, len(inputs)),
        thread_name_prefix="fs2-scientific-acceptance",
    ) as executor:
        for entry in inputs:
            future = executor.submit(
                _invoke_one,
                config,
                entry,
                repository_root=repository_root,
                run_directory=run_directory,
                endpoint=endpoint,
            )
            futures[future] = entry
        for future in as_completed(futures):
            try:
                outcomes.append(future.result())
            except Exception:  # noqa: BLE001 - the aggregate gets only a stable, redacted failure code.
                outcomes.append(
                    ModelOutcome(
                        input=futures[future],
                        status="failed",
                        error_code="acceptance_worker_failed",
                    )
                )

    outcomes.sort(key=lambda item: item.input.model_id)
    succeeded = sum(outcome.status == "succeeded" for outcome in outcomes)
    failed = len(outcomes) - succeeded
    models = [
        _success_entry(outcome)
        if outcome.status == "succeeded"
        else _failure_entry(outcome)
        for outcome in outcomes
    ]
    aggregate = {
        "schema": AGGREGATE_SCHEMA,
        "run_id": config.run_id,
        "endpoint": endpoint,
        "summary": {
            "discovered": len(inputs),
            "primary": sum(
                item.kind == "primary-activation-fragment" for item in inputs
            ),
            "secondary": sum(
                item.kind == "secondary-public-acceptance" for item in inputs
            ),
            "succeeded": succeeded,
            "failed": failed,
            "max_parallel": config.max_parallel,
        },
        "models": models,
    }
    _assert_redacted(aggregate)
    _write_atomic(
        aggregate_path,
        _canonical_json(aggregate, newline=True),
        overwrite=config.overwrite,
    )
    return FleetRun(
        aggregate=aggregate,
        aggregate_path=aggregate_path,
        succeeded=succeeded,
        failed=failed,
    )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-env", default="FS2_INFERENCE_TOKEN")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    config = FleetConfig(
        endpoint=arguments.endpoint,
        repository_root=arguments.repository_root,
        receipt_root=arguments.receipt_root,
        run_id=arguments.run_id or str(uuid4()),
        token_environment=arguments.token_env,
        max_parallel=arguments.max_parallel,
        timeout_seconds=arguments.timeout_seconds,
        poll_seconds=arguments.poll_seconds,
        request_timeout_seconds=arguments.request_timeout_seconds,
        overwrite=arguments.overwrite,
    )
    try:
        result = run_fleet(config)
    except FleetAcceptanceError as error:
        print(f"scientific fleet acceptance failed: {error.code}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "aggregate_receipt": str(result.aggregate_path),
                "failed": result.failed,
                "status": "succeeded" if result.failed == 0 else "failed",
                "succeeded": result.succeeded,
            },
            sort_keys=True,
        )
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
