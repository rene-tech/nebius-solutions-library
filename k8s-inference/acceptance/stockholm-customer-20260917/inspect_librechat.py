#!/usr/bin/env python3
"""Inspect the hosted client and installed skill API, without model/tool calls.

Authenticates only for configuration reads with the endpoint's existing seeded
login. Secrets remain in process memory. No agents, keys or settings are changed.
This proves installation/discovery, NOT use of any skill in an inference turn.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from collect_live import check, digest, now, write_private


def rows(value):
    if isinstance(value, list):
        return value
    for key in ("data", "skills", "items", "agents"):
        if isinstance(value.get(key), list):
            return value[key]
    raise ValueError("unrecognized_collection_shape")


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[credential-redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    process = subprocess.run([
        "/usr/local/bin/nebius", "--profile", args.profile, "--timeout", "25s", "--auth-timeout", "25s",
        "--no-browser", "--no-check-update", "ai", "endpoint", "get", args.endpoint_id, "--format", "json",
    ], capture_output=True, text=True, timeout=40, check=False)
    check(process.returncode == 0, "client_endpoint_read_failed")
    endpoint = json.loads(process.stdout)
    urls = [url for url in endpoint["status"]["public_endpoints"] if url.startswith("https://")]
    check(len(urls) == 1, "client_public_origin_ambiguous")
    origin = urls[0].rstrip("/")
    check(urlsplit(origin).path in ("", "/"), "client_origin_invalid")
    environment = {row["name"]: row.get("value") for row in endpoint["spec"]["environment_variables"]}
    email, password = environment.get("SEED_DEFAULT_USER_EMAIL"), environment.get("SEED_DEFAULT_USER_PASSWORD")
    report = {"schema": "fs2-serve.nebius.ai/stockholm-client-installation/v1", "collected_at": now(),
              "endpoint_id": args.endpoint_id, "project_id": endpoint["metadata"]["parent_id"],
              "image_reference": endpoint["spec"]["image"], "origin": origin,
              "runtime_state": endpoint["status"]["state"],
              "environment_sha256": digest(endpoint["spec"]["environment_variables"]),
              "environment_names": sorted(environment), "skill_execution_proven": False,
              "client_image_digest_verified": "@sha256:" in endpoint["spec"]["image"]}
    secrets = tuple(value for value in environment.values() if isinstance(value, str) and value)
    try:
        check(isinstance(email, str) and isinstance(password, str), "seeded_login_not_available_as_plain_value")
        with httpx.Client(base_url=origin, timeout=30, trust_env=False, follow_redirects=False) as http:
            response = http.post("/api/auth/login", json={"email": email, "password": password})
            check(response.status_code == 200, "client_login_failed")
            auth = response.json()
            token = auth.get("token")
            check(isinstance(token, str) and token, "client_session_token_missing")
            secrets += (token,)
            http.headers["Authorization"] = "Bearer " + token
            config = http.get("/api/config")
            check(config.status_code == 200, "client_configuration_unavailable")
            report["public_configuration_sha256"] = digest(config.json())
            servers_response = http.get("/api/mcp/servers")
            if servers_response.status_code == 200:
                servers = servers_response.json()
                report["mcp_servers"] = {name: {
                    "returned_fields": sorted(server),
                    "url": server.get("url"),
                    "configuration_sha256": digest(server),
                    "custom_user_variable_names": sorted((server.get("customUserVars") or {}).keys()),
                    "authorization_header_configured": "Authorization" in (server.get("headers") or {}),
                    "user_editable": server.get("canEdit"),
                } for name, server in servers.items() if isinstance(server, dict)}
            for path in ("/api/agents/v1/skills", "/api/agents/skills", "/api/skills"):
                response = http.get(path)
                if response.status_code == 200 and "application/json" in response.headers.get("content-type", ""):
                    break
            check(response.status_code == 200 and "application/json" in response.headers.get("content-type", ""),
                  "installed_skill_api_unavailable")
            payload = response.json()
            skills = rows(payload)
            for _ in range(16):
                if not isinstance(payload, dict) or not payload.get("has_more"):
                    break
                check(payload.get("after"), "skill_cursor_missing")
                response = http.get(path, params={"cursor": payload["after"], "limit": 100})
                check(response.status_code == 200, "skill_page_failed")
                payload = response.json()
                skills.extend(rows(payload))
            check(not isinstance(payload, dict) or not payload.get("has_more"), "skill_pagination_truncated")
            check(len({row.get("_id", row.get("id")) for row in skills}) == len(skills), "duplicate_skill")
            report["skills"] = [{"id": str(row.get("_id", row.get("id"))), "name": row.get("name"),
                                 "metadata_sha256": digest(row), "scope": row.get("scope"),
                                 "source": row.get("source")} for row in skills]
            for skill in report["skills"]:
                response = http.get(path + "/" + skill["id"])
                check(response.status_code == 200, "skill_detail_unavailable")
                detail = response.json()
                skill["installed_detail_sha256"] = digest(detail)
                content = detail.get("body", (detail.get("skill") or {}).get("body"))
                skill["installed_body_sha256"] = digest(content) if isinstance(content, str) else None
            report["installed_skills_metadata_sha256"] = digest(report["skills"])
            report["skills_api_path"] = path
            report["outcome"] = "installation_read"
    except Exception as error:
        report.update(outcome="incomplete", failure_type=type(error).__name__)
    # Values such as endpoint URLs are not secrets. Only credential-bearing env
    # variables and session tokens are excluded from the already-sanitized report.
    credential_values = tuple(value for name, value in environment.items()
                              if isinstance(value, str) and value and (
                                  name.endswith("_API_KEY") or name == "SEED_DEFAULT_USER_PASSWORD"))
    excluded = credential_values + ((token,) if "token" in locals() and token else ())
    report = redact(report, excluded)
    write_private(args.output, report, excluded)
    print(json.dumps({"outcome": report["outcome"], "output": str(args.output),
                      "skills": len(report.get("skills", [])), "skill_execution_proven": False}))
    return 0 if report["outcome"] == "installation_read" else 2


if __name__ == "__main__":
    raise SystemExit(main())
