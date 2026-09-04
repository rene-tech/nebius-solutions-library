"""Prove exact Grafana datasources can query their private backends."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import asyncpg

GRAFANA_POSTGRES_DATASOURCE_TYPES = frozenset(
    {
        "grafana-postgresql-datasource",
        "postgres",
    }
)
GRAFANA_PROMETHEUS_DATASOURCE_UID = "prometheus"
GRAFANA_LOKI_DATASOURCE_TYPE = "loki"
GRAFANA_ALERTMANAGER_DATASOURCE_UID = "alertmanager"
GRAFANA_ALERTMANAGER_DATASOURCE_TYPE = "alertmanager"


def grafana_datasource_is_healthy(
    datasource: dict[str, object], health: dict[str, object]
) -> bool:
    return (
        datasource.get("uid") == "fs2-serve-reporting"
        and datasource.get("type") in GRAFANA_POSTGRES_DATASOURCE_TYPES
        and health.get("status") == "OK"
    )


def grafana_datasource_has_identity(
    datasource: dict[str, object], *, uid: str, datasource_type: str
) -> bool:
    return datasource.get("uid") == uid and datasource.get("type") == datasource_type


def prometheus_query_is_one(response: dict[str, object]) -> bool:
    data = response.get("data")
    if response.get("status") != "success" or not isinstance(data, dict):
        return False
    result = data.get("result")
    if data.get("resultType") != "vector" or not isinstance(result, list):
        return False
    if len(result) != 1 or not isinstance(result[0], dict):
        return False
    value = result[0].get("value")
    try:
        return isinstance(value, list) and len(value) == 2 and float(value[1]) == 1.0
    except (TypeError, ValueError):
        return False


def loki_labels_query_succeeded(response: dict[str, object]) -> bool:
    return response.get("status") == "success" and isinstance(
        response.get("data"), list
    )


def alertmanager_status_query_succeeded(response: dict[str, object]) -> bool:
    version_info = response.get("versionInfo")
    cluster = response.get("cluster")
    return (
        isinstance(version_info, dict)
        and isinstance(version_info.get("version"), str)
        and bool(version_info["version"])
        and isinstance(cluster, dict)
        and isinstance(cluster.get("status"), str)
        and bool(cluster["status"])
    )


def grafana(path: str) -> dict[str, object]:
    credential = base64.b64encode(
        f"{os.environ['FS2_GRAFANA_USER']}:{os.environ['FS2_GRAFANA_PASSWORD']}".encode()
    ).decode("ascii")
    request = urllib.request.Request(
        os.environ["FS2_GRAFANA_URL"].rstrip("/") + path,
        headers={"Authorization": f"Basic {credential}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Grafana {path} returned HTTP {response.status}")
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"Grafana {path} returned a non-object")
    return value


def datasource_proxy_path(
    uid: str, backend_path: str, query: dict[str, str] | None = None
) -> str:
    path = "/api/datasources/proxy/uid/{}/{}".format(
        urllib.parse.quote(uid, safe=""),
        backend_path.lstrip("/"),
    )
    return path if query is None else f"{path}?{urllib.parse.urlencode(query)}"


def wait_for_grafana_datasources() -> str:
    run_id = os.environ["FS2_RUN_ID"]
    prometheus_uid = os.environ["FS2_GRAFANA_PROMETHEUS_DATASOURCE_UID"]
    loki_uid = os.environ["FS2_GRAFANA_LOKI_DATASOURCE_UID"]
    alertmanager_uid = os.environ["FS2_GRAFANA_ALERTMANAGER_DATASOURCE_UID"]
    if prometheus_uid != GRAFANA_PROMETHEUS_DATASOURCE_UID:
        raise RuntimeError(
            "Grafana Prometheus datasource UID differs from the exact contract"
        )
    if loki_uid != f"fs2-{run_id}-loki":
        raise RuntimeError(
            "Grafana Loki datasource UID differs from the run-scoped contract"
        )
    if alertmanager_uid not in {"", GRAFANA_ALERTMANAGER_DATASOURCE_UID}:
        raise RuntimeError(
            "Grafana Alertmanager datasource UID differs from the chart-owned contract"
        )

    last_error: Exception | None = None
    for _ in range(60):
        try:
            reporting = grafana("/api/datasources/uid/fs2-serve-reporting")
            reporting_health = grafana(
                "/api/datasources/uid/fs2-serve-reporting/health"
            )
            if not grafana_datasource_is_healthy(reporting, reporting_health):
                raise RuntimeError("Grafana reporting datasource is not healthy")

            prometheus = grafana(f"/api/datasources/uid/{prometheus_uid}")
            if not grafana_datasource_has_identity(
                prometheus, uid=prometheus_uid, datasource_type="prometheus"
            ):
                raise RuntimeError("Grafana Prometheus datasource identity differs")
            prometheus_result = grafana(
                datasource_proxy_path(
                    prometheus_uid,
                    "/api/v1/query",
                    {"query": "vector(1)"},
                )
            )
            if not prometheus_query_is_one(prometheus_result):
                raise RuntimeError("Grafana-proxied Prometheus vector query failed")

            loki = grafana(f"/api/datasources/uid/{loki_uid}")
            loki_health = grafana(f"/api/datasources/uid/{loki_uid}/health")
            if (
                not grafana_datasource_has_identity(
                    loki, uid=loki_uid, datasource_type=GRAFANA_LOKI_DATASOURCE_TYPE
                )
                or loki_health.get("status") != "OK"
            ):
                raise RuntimeError("Grafana Loki datasource identity or health differs")
            loki_result = grafana(
                datasource_proxy_path(loki_uid, "/loki/api/v1/labels")
            )
            if not loki_labels_query_succeeded(loki_result):
                raise RuntimeError("Grafana-proxied Loki labels query failed")

            if alertmanager_uid:
                alertmanager = grafana(f"/api/datasources/uid/{alertmanager_uid}")
                if not grafana_datasource_has_identity(
                    alertmanager,
                    uid=alertmanager_uid,
                    datasource_type=GRAFANA_ALERTMANAGER_DATASOURCE_TYPE,
                ):
                    raise RuntimeError(
                        "Grafana Alertmanager datasource identity differs"
                    )
                # Alertmanager is a frontend-only built-in Grafana datasource.
                # The generic /health endpoint requires a backend plugin and
                # returns "Plugin unavailable" even when proxying is healthy.
                alertmanager_result = grafana(
                    datasource_proxy_path(alertmanager_uid, "/api/v2/status")
                )
                if not alertmanager_status_query_succeeded(alertmanager_result):
                    raise RuntimeError(
                        "Grafana-proxied Alertmanager status query failed"
                    )
            return "query-ready" if alertmanager_uid else "disabled"
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as error:
            last_error = error
            time.sleep(5)
    raise RuntimeError(
        "Grafana datasources did not pass backend queries"
    ) from last_error


async def main() -> None:
    alertmanager_status = await asyncio.to_thread(wait_for_grafana_datasources)
    connection = await asyncpg.connect(
        os.environ["FS2_REPORTING_DATABASE_URL"], timeout=20
    )
    try:
        count = await connection.fetchval(
            "SELECT count(*) FROM fs2_reporting_model_usage"
        )
        if not isinstance(count, int) or count < 0:
            raise RuntimeError("reporting attribution view returned an invalid count")
    finally:
        await connection.close()
    print(
        json.dumps(
            {
                "alertmanager_datasource": alertmanager_status,
                "loki_datasource": "query-ready",
                "prometheus_datasource": "query-ready",
                "reporting_datasource": "ready",
                "rows": count,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
