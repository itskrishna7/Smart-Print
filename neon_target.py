"""Verify that a direct PostgreSQL URL belongs to an explicitly selected Neon branch."""

import json
import os
import re
import subprocess
from urllib.parse import urlsplit


class NeonTargetError(RuntimeError):
    """Raised when the target URL cannot be tied to the requested Neon branch."""


_NEON_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,60}$")


def verify_neon_target(
    database_url: str,
    target_environment: str,
    project_id: str,
    branch_id: str,
) -> None:
    if target_environment not in ("test", "production"):
        raise NeonTargetError("A valid target environment is required.")
    if not _NEON_ID_PATTERN.fullmatch(project_id) or not _NEON_ID_PATTERN.fullmatch(branch_id):
        raise NeonTargetError("A valid Neon project ID and branch ID are required.")

    environment_prefix = f"SMARTPRINT_NEON_{target_environment.upper()}"
    expected_project_id = os.getenv(f"{environment_prefix}_PROJECT_ID", "").strip()
    expected_branch_id = os.getenv(f"{environment_prefix}_BRANCH_ID", "").strip()
    if not expected_project_id or not expected_branch_id:
        raise NeonTargetError(
            f"Set {environment_prefix}_PROJECT_ID and {environment_prefix}_BRANCH_ID "
            "to allow this target environment."
        )
    if (project_id, branch_id) != (expected_project_id, expected_branch_id):
        raise NeonTargetError(
            f"The selected Neon project and branch do not match the {target_environment} target allowlist."
        )

    parsed_url = urlsplit(database_url)
    hostname = (parsed_url.hostname or "").rstrip(".").lower()
    if parsed_url.scheme not in ("postgres", "postgresql") or not hostname:
        raise NeonTargetError("The target must be a PostgreSQL connection URL.")
    if "-pooler." in hostname:
        raise NeonTargetError("Use the direct, unpooled Neon URL for migrations.")

    neon_cli = "neon.cmd" if os.name == "nt" else "neon"
    command = [
        neon_cli,
        "api",
        f"/projects/{project_id}/branches/{branch_id}/endpoints",
        "-o",
        "json",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NeonTargetError(
            "Could not verify the Neon target. Install and authenticate the Neon CLI, then retry."
        ) from error

    if result.returncode:
        raise NeonTargetError(
            "Could not verify the Neon target. Check Neon CLI authentication and project access."
        )
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise NeonTargetError("Neon returned invalid endpoint metadata; no changes were made.") from error

    endpoints = response.get("endpoints") if isinstance(response, dict) else None
    valid_hosts = set()
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            if (
                endpoint.get("project_id") != project_id
                or endpoint.get("branch_id") != branch_id
                or endpoint.get("type") != "read_write"
            ):
                continue
            hosts = [endpoint.get("host")]
            endpoint_hosts = endpoint.get("hosts")
            if isinstance(endpoint_hosts, dict):
                hosts.append(endpoint_hosts.get("read_write_host"))
            group = endpoint.get("group")
            computes = group.get("computes") if isinstance(group, dict) else None
            if isinstance(computes, list):
                hosts.extend(
                    compute.get("compute_host")
                    for compute in computes
                    if isinstance(compute, dict)
                )
            valid_hosts.update(
                host.rstrip(".").lower()
                for host in hosts
                if isinstance(host, str) and host.strip()
            )

    if hostname not in valid_hosts:
        raise NeonTargetError(
            "DATABASE_URL_UNPOOLED does not match a read-write endpoint on the selected Neon branch; "
            "no changes were made."
        )
