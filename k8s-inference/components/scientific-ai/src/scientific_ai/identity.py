"""Per-execution AWS SDK federation, not an application credential issuer.

Only an operator-authorized customer access profile may supply these values.
S2 mounts the current attempt's projected token; AWS trust/IAM enforce scope.
Each call constructs an isolated refreshable provider with no ambient fallback.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import AssumeRoleWithWebIdentityProvider, CredentialResolver


def web_identity_session(*, role_arn: str, token_file: Path, session_name: str, region: str):
    if not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[\w+=,.@/-]+", role_arn):
        raise ValueError("an authorized AWS role is required")
    if not token_file.is_absolute() or not re.fullmatch(r"[\w+=,.@-]{2,64}", session_name):
        raise ValueError("use the mounted token file and an opaque per-attempt session name")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region):
        raise ValueError("an explicit AWS region is required")
    session = botocore.session.Session()
    session.set_config_variable("region", region)
    session.set_config_variable("sts_regional_endpoints", "regional")
    session.set_config_variable("ignore_configured_endpoint_urls", True)
    configuration = {
        "profiles": {
            "scientific-ai-job": {
                "role_arn": role_arn,
                "web_identity_token_file": str(token_file),
                "role_session_name": session_name,
            }
        }
    }
    provider = AssumeRoleWithWebIdentityProvider(
        load_config=lambda: configuration,
        client_creator=session.create_client,
        profile_name="scientific-ai-job",
        disable_env_vars=True,
    )
    # Not the default resolver: environment keys, shared user files, EC2
    # metadata and container-role credentials are not alternate authority.
    session.register_component("credential_provider", CredentialResolver([provider]))
    return boto3.Session(botocore_session=session)


def s3_client(session, *, endpoint_url: str | None = None):
    """The optional endpoint must come from protected operator configuration."""
    if endpoint_url is not None:
        url = urlsplit(endpoint_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("storage endpoint must be an approved HTTPS origin")
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "assume-role-with-web-identity":
        raise PermissionError("this AWS path requires the scoped refreshable workload-identity provider")
    return session.client(
        "s3",
        endpoint_url=endpoint_url,
        config=Config(
            signature_version="s3v4",
            retries={"mode": "standard", "max_attempts": 4},
            connect_timeout=10,
            read_timeout=60,
        ),
    )
