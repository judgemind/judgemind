"""Shared OpenSearch client factory with SigV4-signed requests.

SigV4 is the preferred (deployed) auth path: the client signs requests with
the ambient AWS credentials (the ECS task role in deployed environments) so
the OpenSearch domain's resource-based access policy can re-tighten to
enumerated role ARNs instead of ``Principal: AWS = "*"`` (#4040).

Auth selection (in priority order):
    1. Local-dev basic-auth fallback — ``OPENSEARCH_USERNAME`` +
       ``OPENSEARCH_PASSWORD`` both set and non-empty.  Keeps
       ``scripts/rebuild_db.sh`` working against the Docker-Compose OpenSearch,
       which does not speak SigV4.
    2. SigV4 — built from the boto3/botocore default credential chain.
    3. No-auth — local-dev with no creds and no basic-auth env set; preserves
       today's behavior for the unset-local path.

Usage:
    from framework.opensearch_client import make_opensearch_client
    client = make_opensearch_client(opensearch_url)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "us-west-2"
_SERVICE_NAME = "es"


def _resolve_region(region: str | None, session: boto3.Session) -> str:
    """Resolve the AWS region: arg > env > botocore session > default."""
    if region:
        return region
    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region:
        return env_region
    if session.region_name:
        return session.region_name
    return _DEFAULT_REGION


def make_opensearch_client(
    hosts: str | list[str],
    *,
    timeout: int = 30,
    max_retries: int = 3,
    retry_on_timeout: bool = True,
    region: str | None = None,
) -> OpenSearch:
    """Build an OpenSearch client, preferring SigV4-signed requests.

    ``hosts`` accepts a single URL string or a list; it is normalized to a
    list internally.  The ``timeout``/``max_retries``/``retry_on_timeout``
    knobs default to the values that make rebuilds self-healing under load
    (opensearchpy otherwise defaults to a 10s read_timeout and no retries,
    producing sporadic ``ConnectionTimeout`` failures — see #2481).
    """
    host_list = [hosts] if isinstance(hosts, str) else list(hosts)

    base_kwargs: dict[str, Any] = {
        "hosts": host_list,
        "timeout": timeout,
        "max_retries": max_retries,
        "retry_on_timeout": retry_on_timeout,
    }

    os_user = os.environ.get("OPENSEARCH_USERNAME", "")
    os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
    if os_user and os_pass:
        # local-dev fallback: HTTP Basic auth against the Docker-Compose
        # OpenSearch (which does not speak SigV4).  Excluded from the #4040
        # http_auth grep because it is gated behind explicit env vars.
        base_kwargs["http_auth"] = (os_user, os_pass)
        return OpenSearch(**base_kwargs)

    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        # No basic-auth env and no resolvable AWS credentials (local-dev with
        # neither configured).  Preserve today's no-auth behavior.
        logger.warning(
            "No OpenSearch basic-auth env and no AWS credentials resolvable; "
            "building no-auth OpenSearch client (local-dev path)."
        )
        return OpenSearch(**base_kwargs)

    resolved_region = _resolve_region(region, session)
    signer = AWSV4SignerAuth(credentials, resolved_region, _SERVICE_NAME)
    base_kwargs["http_auth"] = signer
    base_kwargs["connection_class"] = RequestsHttpConnection
    return OpenSearch(**base_kwargs)
