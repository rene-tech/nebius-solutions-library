#!/usr/bin/env python3
"""Independent, read-only post-retirement history/service verification."""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import yaml

EXPECTED = "sha256:803227d1b63342be5176976c98820d4bb38edb4d1dc777187d7c89bba8551674"
POD = (
    r"""
import asyncpg,asyncio,os,json
from fs2_serve.postgres import PostgresStore
async def m():
 pool=await asyncpg.create_pool(os.environ['FS2_DATABASE_URL'],min_size=1,max_size=1,command_timeout=30)
 async with pool.acquire() as c,c.transaction(readonly=True):
  tenant='stockholm'
  ids=await c.fetch('SELECT id,status,model_id FROM fs2_operations WHERE tenant_id=$1 ORDER BY id',tenant)
  keys=await c.fetch('SELECT id,revoked_at FROM fs2_tokens WHERE tenant_id=$1',tenant)
  retired=await c.fetchrow('SELECT * FROM fs2_retired_tenants WHERE tenant_id=$1',tenant)
  result={'retirement':dict(retired) if retired else None,
    'configured_users':await c.fetchval('SELECT count(*) FROM fs2_inference_users WHERE tenant_id=$1',tenant),
    'retained_operations':[dict(r) for r in ids], 'retained_keys':len(keys),
    'all_keys_revoked':all(r['revoked_at'] for r in keys),
    'keys_resolvable_for_auth':await c.fetchval("""
    + repr("""SELECT count(*) FROM fs2_tokens t WHERE tenant_id=$1
      AND NOT EXISTS(SELECT 1 FROM fs2_retired_tenants r WHERE r.tenant_id=t.tenant_id)""")
    + r""",tenant),
    'storage_buckets':await c.fetchval('SELECT count(*) FROM fs2_storage_buckets WHERE tenant_id=$1',tenant),
    'request_debug_rows':await c.fetchval('SELECT count(*) FROM fs2_request_debug WHERE tenant_id=$1',tenant),
    'terminal_fact_rows':await c.fetchval('SELECT count(*) FROM fs2_usage_facts WHERE tenant_id=$1',tenant)}
  print(json.dumps(result,default=str))
 await pool.close()
asyncio.run(m())
"""
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    k = [
        "kubectl",
        "--kubeconfig",
        a.kubeconfig,
        "--context",
        a.context,
        "--request-timeout=30s",
    ]

    def query(*cmd):
        return json.loads(
            subprocess.check_output(k + list(cmd), stderr=subprocess.PIPE)
        )

    database = json.loads(
        subprocess.check_output(
            k
            + [
                "-n",
                "fs2-system",
                "exec",
                "-i",
                "deploy/fs2-serve-control-plane",
                "-c",
                "control-plane",
                "--",
                "python",
                "-",
            ],
            input=POD.encode(),
            stderr=subprocess.PIPE,
        )
    )
    assert (
        database["configured_users"] == 0
        and database["all_keys_revoked"]
        and database["keys_resolvable_for_auth"] == 0
    )
    assert (
        database["retained_keys"] == 22 and len(database["retained_operations"]) == 140
    )
    assert database["terminal_fact_rows"] == 149 and database["storage_buckets"] == 0
    jobs = {}
    for attempt in range(4):
        for row in query("-n", "fs2-system", "get", "jobs", "-o", "json")["items"]:
            if row["metadata"]["name"].startswith(
                "fs2-serve-control-plane-maintenance-"
            ):
                if (
                    row["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
                        "@" + EXPECTED
                    )
                    and row["status"].get("succeeded") == 1
                ):
                    jobs[row["metadata"]["name"]] = {
                        "started_at": row["status"]["startTime"],
                        "completed_at": row["status"]["completionTime"],
                    }
        if len(jobs) >= 3:
            break
        time.sleep(35)
    assert len(jobs) >= 3, "three natural maintenance completions not observed"
    deps = query("-n", "fs2-system", "get", "deployments", "-o", "json")
    services = []
    for row in deps["items"]:
        if row["metadata"]["name"] in (
            "fs2-serve-control-plane",
            "fs2-serve-control-plane-model-controller",
            "fs2-serve-control-plane-admin-console",
        ):
            assert row["status"].get("readyReplicas") == row["spec"]["replicas"]
            services.append(
                {
                    "name": row["metadata"]["name"],
                    "ready": row["status"]["readyReplicas"],
                    "image": row["spec"]["template"]["spec"]["containers"][0]["image"],
                }
            )
    prom = query(
        "get",
        "--raw",
        "/api/v1/namespaces/fs2-observability/services/http:fs2-r927c465c6d-monitoring-prometheus:9090/proxy/api/v1/rules",
    )
    rules = [
        {"name": r["name"], "health": r.get("health"), "state": r.get("state")}
        for g in prom["data"]["groups"]
        for r in g["rules"]
        if r.get("name", "").startswith("Fs2Serve")
    ]
    alert = query(
        "get",
        "--raw",
        "/api/v1/namespaces/fs2-observability/services/http:fs2-r927c465c6d-monitoring-alertmanager:9093/proxy/api/v2/status",
    )
    cfg = yaml.safe_load(alert["config"]["original"])
    receivers = [
        {"name": r["name"], "integrations": [x for x in r if x.endswith("_configs")]}
        for r in cfg["receivers"]
    ]
    with httpx.Client(timeout=30, trust_env=False) as client:
        public = {
            path: client.get("https://89.169.99.188" + path).status_code
            for path in ("/readyz", "/admin/")
        }
    assert all(code == 200 for code in public.values())
    result = {
        "at": datetime.now(UTC).isoformat(),
        "database": database,
        "services": services,
        "maintenance": jobs,
        "public": public,
        "rules": rules,
        "alert_receivers": receivers,
        "full_customer_qualification": False,
    }
    with os.fdopen(
        os.open(a.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
    ) as f:
        json.dump(result, f, indent=2)
    print(
        json.dumps(
            {
                "output": str(a.output),
                "retained_operations": len(database["retained_operations"]),
                "deleted_accounts_verified": database["configured_users"] == 0,
                "maintenance_intervals": len(jobs),
                "services_ready": True,
                "alert_receivers": receivers,
            }
        )
    )


if __name__ == "__main__":
    main()
