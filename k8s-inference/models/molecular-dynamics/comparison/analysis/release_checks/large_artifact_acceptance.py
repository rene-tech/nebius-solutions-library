#!/usr/bin/env python3
"""Bounded read-only acceptance of existing large customer artifact streams.

No simulation, artifact upload, key creation, configuration or quota mutation.
The optional operator session is created and closed only to inspect our own
request metadata. Neither scientific bytes nor credentials are persisted.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx

ARTIFACTS = (
    {"id": "86972296-08dc-4c1c-bba1-343af36b14c6", "size": 465681353, "sha256": "1534e59c6fca58f4a43e1d80f47823029df30890b2cafdff574339634b42bf2a"},
    {"id": "ae654dd5-eaf4-45ba-9c6c-f1207ab46575", "size": 321192501, "sha256": "322c5ccec16e62bd5e130f510bcde29d61e58a7b04ea8be64e6239af81baa491"},
)
OPERATION = "46ea947b-fecc-4036-9c87-42df6886f1a6"
CHUNK = 65536


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def new_client(origin, **kwargs):
    return httpx.Client(base_url=origin, timeout=httpx.Timeout(15., connect=10.),
                        trust_env=False, verify=True, follow_redirects=False, **kwargs)


def validate_headers(response, artifact):
    if response.status_code != 200:
        raise ValueError("unexpected_http_status")
    expected = {"x-fs2-artifact-id": artifact["id"], "x-fs2-artifact-sha256": artifact["sha256"], "content-length": str(artifact["size"]), "x-fs2-artifact-size-bytes": str(artifact["size"])}
    if any(response.headers.get(key) != value for key, value in expected.items()):
        raise ValueError("artifact_response_identity_mismatch")
    if response.headers.get("content-encoding") not in (None, "identity"):
        raise ValueError("artifact_bytes_must_not_be_transparently_decoded")


def download(origin, key, artifact, *, partial=False, barrier=None, transport=None, max_seconds=180):
    request_id = str(uuid4())
    record = {"artifact": artifact, "request_id": request_id, "qualification_id": request_id, "mode": "intentional-partial-close" if partial else "full-stream-retry", "started_at": now(), "received_bytes": 0, "verified": False, "scientific_bytes_retained": False}
    digest, started = hashlib.sha256(), time.monotonic()
    try:
        with new_client(origin, transport=transport) as client:
            if barrier:
                barrier.wait(timeout=20)
            with client.stream("GET", f"/v1/artifacts/{artifact['id']}/content", headers={"authorization": "Bearer " + key, "x-request-id": request_id, "x-fs2-qualification-id": request_id, "accept-encoding": "identity"}) as response:
                record["http_status"] = response.status_code
                returned = response.headers.get("x-request-id")
                if returned and re.fullmatch(r"[a-fA-F0-9-]{36}", returned):
                    record["response_request_id"] = returned
                validate_headers(response, artifact)
                for block in response.iter_raw(chunk_size=CHUNK):
                    record.setdefault("first_byte_at", now())
                    if time.monotonic() - started > max_seconds:
                        raise TimeoutError("bounded_download_deadline")
                    digest.update(block)
                    record["received_bytes"] += len(block)
                    if record["received_bytes"] > artifact["size"]:
                        raise ValueError("artifact_longer_than_expected")
                    if partial and record["received_bytes"] >= 1024 * 1024:
                        record["intentional_close"] = True
                        break
                record["observed_sha256"] = digest.hexdigest()
                if not partial:
                    record["verified"] = record["received_bytes"] == artifact["size"] and digest.hexdigest() == artifact["sha256"]
                    if not record["verified"]:
                        raise ValueError("artifact_size_or_digest_mismatch")
    except Exception as error:
        # Never export exception text, request headers, response body or URLs.
        record["error_type"] = type(error).__name__
        if isinstance(error, ValueError):
            record["error_code"] = str(error)
    record.update(finished_at=now(), wall_seconds=time.monotonic() - started, observed_sha256=digest.hexdigest())
    return record


def bounded_json(client, path, headers=None, params=None):
    with client.stream("GET", path, headers=headers, params=params) as response:
        if response.status_code != 200:
            raise ValueError("read_http_status_" + str(response.status_code))
        body = bytearray()
        for chunk in response.iter_bytes(CHUNK):
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise ValueError("read_response_exceeds_1MiB")
        return json.loads(body)


def poll_http(origin, key, stop, records):
    with new_client(origin) as client:
        while not stop.is_set():
            for path in ("/readyz", f"/v1/operations/{OPERATION}"):
                row, start = {"at": now(), "endpoint": path}, time.monotonic()
                try:
                    value = bounded_json(client, path, {"authorization": "Bearer " + key} if path.startswith("/v1/") else None)
                    status = value.get("operation", value).get("status")
                    row.update(http_status=200, observed_status=status, passed=status == ("ready" if path == "/readyz" else "succeeded"))
                except Exception as error:
                    row.update(passed=False, error_type=type(error).__name__)
                row["latency_seconds"] = time.monotonic() - start
                records.append(row)
            stop.wait(2)


def kubectl(args, *command):
    result = subprocess.run(["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "--request-timeout=15s", *command], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("kubectl_read_failed")
    return json.loads(result.stdout)


def memory_bytes(quantity):
    match = re.fullmatch(r"([0-9.]+)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(quantity))
    if not match:
        raise ValueError("unsupported_memory_quantity")
    suffix = match[2] or ""
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4}[suffix]
    return int(float(match[1]) * (1024 if suffix.endswith("i") else 1000) ** power)


def pod_snapshot(args, selector):
    listing = kubectl(args, "-n", "fs2-system", "get", "pods", "-l", selector, "-o", "json")
    pods = []
    for pod in listing["items"]:
        if not pod["metadata"]["name"].startswith("fs2-serve-control-plane-"):
            raise ValueError("selector_includes_unrelated_pod")
        pods.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "deleting": pod["metadata"].get("deletionTimestamp"),
                     "containers": [{key: container[key] for key in ("name", "image", "resources")} for container in pod["spec"]["containers"]],
                     "statuses": [{key: status.get(key) for key in ("name", "ready", "restartCount", "imageID", "state", "lastState")} for status in pod["status"].get("containerStatuses", [])]})
    return {"at": now(), "pods": pods}


def memory_snapshot(args, selector):
    raw = kubectl(args, "get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/fs2-system/pods?" + urlencode({"labelSelector": selector}))
    rows = []
    for pod in raw["items"]:
        if not pod["metadata"]["name"].startswith("fs2-serve-control-plane-"):
            raise ValueError("memory_selector_includes_unrelated_pod")
        rows.append({"pod": pod["metadata"]["name"], "timestamp": pod.get("timestamp"), "window": pod.get("window"), "containers": [{"name": item["name"], "memory": item["usage"]["memory"], "memory_bytes": memory_bytes(item["usage"]["memory"]), "cpu": item["usage"]["cpu"]} for item in pod["containers"]]})
    return {"at": now(), "metrics": rows, "scope": "metrics-server sampled container memory; not a continuously measured peak"}


def poll_memory(args, selector, stop, records):
    while not stop.is_set():
        try:
            records.append(memory_snapshot(args, selector))
        except Exception as error:
            records.append({"at": now(), "error_type": type(error).__name__})
        stop.wait(5)


def ready_exact(snapshot, image):
    return len(snapshot["pods"]) == 3 and all(not p["deleting"] and p["containers"] and all(c["image"] == image and c["resources"]["limits"]["memory"] == "2Gi" for c in p["containers"]) and p["statuses"] and all(s["ready"] for s in p["statuses"]) for p in snapshot["pods"])


def restart_deltas(before, after):
    def counters(snapshot):
        return {(p["uid"], c["name"]): c["restartCount"] for p in snapshot["pods"] for c in p["statuses"]}
    first, second = counters(before), counters(after)
    return {"same_pod_container_identities": set(first) == set(second), "deltas": [{"pod_uid": uid, "container": name, "restart_delta": second.get((uid, name), -1) - count} for (uid, name), count in first.items()]}


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("debug_correlation_requires_timezone")
    return parsed


def sanitize_debug(detail, transfer, method, tenant, principal):
    if detail.get("tenant_id") != tenant or detail.get("principal_id") != principal:
        raise ValueError("debug_row_owner_or_request_mismatch")
    if detail.get("endpoint") != f"/v1/artifacts/{transfer['artifact']['id']}/content":
        raise ValueError("debug_row_not_owned_target_artifact")
    body = detail["response_body"]
    keys = ("id", "request_id", "tenant_id", "principal_id", "endpoint", "http_status", "started_at", "completed_at", "disconnected")
    metadata = {key: body.get(key) for key in ("capture_mode", "observed_bytes", "complete")}
    metadata["artifact_reference"] = {key: (body.get("artifact_reference") or {}).get(key) for key in ("artifact_id", "sha256", "size_bytes", "observed_sha256", "delivered_bytes", "verified")}
    return {**{key: detail.get(key) for key in keys}, "client_request_id": transfer["request_id"], "correlation": {"method": method, "unique_owned_target_candidates": 1, "client_started_at": transfer["started_at"], "client_finished_at": transfer["finished_at"], "client_qualification_id": transfer.get("qualification_id")}, "response_body_metadata": metadata, "body_data_character_count": len(body.get("data", "")), "headers_query_body_and_error_text_exported": False}


def correlate_debug(details, transfers, tenant, principal):
    """Require a unique owned match; never assume X-Request-ID survives ingress.

    New requests must retain the distinct qualification header. The previously
    recorded cohort, which predates that header, can only match a unique exact
    artifact path with server start inside its client request interval. Body
    success is deliberately NOT used to select a candidate.
    """
    rows, used = [], set()
    for transfer in transfers:
        candidates = [detail for detail in details if detail.get("tenant_id") == tenant and detail.get("principal_id") == principal and detail.get("endpoint") == f"/v1/artifacts/{transfer['artifact']['id']}/content"]
        candidates = [detail for detail in candidates if parse_time(transfer["started_at"]) <= parse_time(detail["started_at"]) <= parse_time(transfer["finished_at"])]
        if transfer.get("qualification_id"):
            method = "unique-owner-artifact-start-window-and-x-fs2-qualification-id"
            candidates = [detail for detail in candidates if any(key.lower() == "x-fs2-qualification-id" and value == transfer["qualification_id"] for key, value in detail.get("request_headers", []))]
        else:
            method = "legacy-unique-owner-artifact-server-start-within-client-interval"
        if len(candidates) > 1:
            raise ValueError("ambiguous_owned_debug_exchange")
        if not candidates:
            continue
        detail = candidates[0]
        if detail["id"] in used:
            raise ValueError("debug_exchange_cannot_match_multiple_transfers")
        used.add(detail["id"])
        rows.append(sanitize_debug(detail, transfer, method, tenant, principal))
    return rows


def inspect_debug(args, transfers, started_at):
    secret = kubectl(args, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json")
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    ids = {t["request_id"] for t in transfers}
    endpoints = {f"/v1/artifacts/{t['artifact']['id']}/content" for t in transfers}
    details, rows = {}, []
    end = (max(datetime.fromisoformat(t["finished_at"]) for t in transfers) + timedelta(seconds=1)).isoformat()
    pages = 0
    with new_client(args.origin, headers={"origin": args.origin}) as client:
        response = client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
        if response.status_code not in (200, 201):
            raise RuntimeError("operator_session_unavailable")
        try:
            for attempt in range(5):
                cursor = None
                for page in range(5):
                    params = {"from": started_at, "to": end, "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    listing = bounded_json(client, "/admin/api/v1/requests", params=params)["data"]
                    pages += 1
                    for row in listing["items"]:
                        if row.get("tenant_id") != args.tenant or row.get("principal_id") != args.principal or row.get("endpoint") not in endpoints:
                            continue
                        detail = bounded_json(client, "/admin/api/v1/requests/" + row["id"])["data"]
                        details[detail["id"]] = detail
                    cursor = listing.get("next_cursor")
                    if not cursor:
                        break
                if cursor:
                    raise ValueError("debug_window_exceeds_bounded_pagination")
                rows = correlate_debug(list(details.values()), transfers, args.tenant, args.principal)
                if len(rows) == len(ids):
                    break
                time.sleep(2)
        finally:
            client.delete("/admin/api/v1/session")
    return {"requested_rows": len(ids), "found_rows": len(rows), "owned_target_rows_in_window": len(details), "rows": rows, "list_pages_inspected": pages, "request_start_window": {"from": started_at, "to": end}}


def debug_pass(debug, transfers):
    rows = {row["client_request_id"]: row for row in debug["rows"]}
    for transfer in transfers:
        row = rows.get(transfer["request_id"])
        if row is None or row["body_data_character_count"] > 1024 or row.get("http_status") != 200:
            return False
        body, artifact = row["response_body_metadata"], transfer["artifact"]
        ref = body.get("artifact_reference") or {}
        if body["capture_mode"] != "artifact_reference" or ref.get("artifact_id") != artifact["id"] or ref.get("sha256") != artifact["sha256"] or ref.get("size_bytes") != artifact["size"]:
            return False
        if transfer["mode"] == "intentional-partial-close":
            if ref.get("verified") is not False or body["complete"] is not False or not 0 < ref.get("delivered_bytes", 0) < artifact["size"]:
                return False
        elif ref.get("verified") is not True or not body["complete"] or ref.get("delivered_bytes") != artifact["size"] or ref.get("observed_sha256") != artifact["sha256"]:
            return False
    return True


def post_transfer_memory(args, selector, record):
    end = max(datetime.fromisoformat(row["finished_at"]) for row in record["full_downloads"])
    for attempt in range(10):
        sample = memory_snapshot(args, selector)
        record["memory_samples"].append(sample)
        fresh = len(sample["metrics"]) == 3 and all(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) >= end for row in sample["metrics"])
        if fresh:
            record["memory_after"] = sample
            record["memory_after_fresh"] = True
            return
        time.sleep(5)
    record["memory_after_fresh"] = False


def cohort_passed(record, image):
    return bool(record["partial"].get("intentional_close") and all(row["verified"] for row in record["full_downloads"]) and record["http_polls"] and all(row["passed"] for row in record["http_polls"]) and record["memory_samples"] and all("metrics" in row for row in record["memory_samples"]) and record.get("memory_after_fresh") and ready_exact(record["after"], image) and record["restart_check"]["same_pod_container_identities"] and all(row["restart_delta"] == 0 for row in record["restart_check"]["deltas"]) and record["debug_passed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--tenant", default="md-qualification-20260923")
    parser.add_argument("--principal", default="md-test")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-receipt", type=Path, help="Recheck retained first-cohort metadata and run only the unfinished second cohort.")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "fs2-large-artifact-readonly-acceptance/v1", "status": "incomplete", "started_at": now(), "origin": args.origin, "expected_image": args.expected_image, "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "artifacts": ARTIFACTS, "operation_id": OPERATION, "credentials_or_scientific_bytes_persisted": False, "cohorts": []}
    try:
        deployment = kubectl(args, "-n", "fs2-system", "get", "deployment", "fs2-serve-control-plane", "-o", "json")
        selector = ",".join(f"{key}={value}" for key, value in sorted(deployment["spec"]["selector"]["matchLabels"].items()))
        key = json.loads(args.key_file.read_text())["secret"]
        if args.resume_receipt:
            prior = json.loads(args.resume_receipt.read_text())
            if prior.get("expected_image") != args.expected_image or prior.get("origin") != args.origin or len(prior.get("cohorts", [])) != 1:
                raise ValueError("resume_receipt_identity_or_cohort_count_differs")
            record = prior["cohorts"][0]
            if not all(row["verified"] for row in record.get("full_downloads", [])) or len(record.get("full_downloads", [])) != 2:
                raise ValueError("resume_requires_existing_complete_verified_download_pair")
            report["resumed_from"] = {"path": str(args.resume_receipt), "sha256": hashlib.sha256(args.resume_receipt.read_bytes()).hexdigest(), "scope": "new metadata correlation only; original failed receipt retained; completed downloads are not repeated"}
            record["prior_debug_lookup"] = record["debug"]
            transfers = [record["partial"], *record["full_downloads"]]
            record["debug"] = inspect_debug(args, transfers, record["started_at"])
            record["debug_passed"] = debug_pass(record["debug"], transfers)
            record["metadata_rechecked_at"] = now()
            record["memory_recheck_scope"] = "Fresh after-sample is a later metadata reconciliation, not contemporaneous transfer peak memory. Original sampled timestamps are retained."
            post_transfer_memory(args, selector, record)
            record["passed"] = cohort_passed(record, args.expected_image)
            report["cohorts"].append(record)
            save(args.output / "cohort-1-reconciled.json", record)
            if not record["passed"]:
                raise ValueError("existing_cohort_metadata_recheck_failed")
        for number in range(len(report["cohorts"]) + 1, 3):
            record = {"cohort": number, "started_at": now(), "before": pod_snapshot(args, selector), "http_polls": [], "memory_samples": []}
            report["cohorts"].append(record)
            if not ready_exact(record["before"], args.expected_image):
                raise ValueError("exact_three_ready_new_image_2Gi_precondition_failed")
            stop = threading.Event()
            http_thread = threading.Thread(target=poll_http, args=(args.origin, key, stop, record["http_polls"]), daemon=True)
            memory_thread = threading.Thread(target=poll_memory, args=(args, selector, stop, record["memory_samples"]), daemon=True)
            http_thread.start()
            memory_thread.start()
            try:
                record["partial"] = download(args.origin, key, ARTIFACTS[0], partial=True)
                barrier = threading.Barrier(2)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending = [pool.submit(download, args.origin, key, artifact, barrier=barrier) for artifact in ARTIFACTS]
                    record["full_downloads"] = [future.result() for future in pending]
                # Observe health and metrics after transfer as well as during it.
                stop.wait(10)
            finally:
                stop.set()
                http_thread.join(timeout=35)
                memory_thread.join(timeout=25)
            record["after"] = pod_snapshot(args, selector)
            record["restart_check"] = restart_deltas(record["before"], record["after"])
            transfers = [record["partial"], *record["full_downloads"]]
            record["debug"] = inspect_debug(args, transfers, record["started_at"])
            record["debug_passed"] = debug_pass(record["debug"], transfers)
            post_transfer_memory(args, selector, record)
            record["passed"] = cohort_passed(record, args.expected_image)
            save(args.output / f"cohort-{number}.json", record)
            print(json.dumps({"cohort": number, "passed": record["passed"], "full_bytes_verified": sum(row["received_bytes"] for row in record["full_downloads"] if row["verified"]), "debug_passed": record["debug_passed"], "health_polls": len(record["http_polls"])}), flush=True)
            if not record["passed"]:
                raise ValueError("acceptance_gate_failed_no_automatic_repetition")
        report["status"] = "two-readonly-concurrent-download-cohorts-passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, ValueError):
            report["error_code"] = str(error)
        raise
    finally:
        report["finished_at"] = now()
        save(args.output / "receipt.json", report)


if __name__ == "__main__":
    main()
