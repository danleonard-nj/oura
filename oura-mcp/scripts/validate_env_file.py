#!/usr/bin/env python3
"""Check an env file before it is pushed to Kubernetes as a Secret.

The point is to catch values that will break the deployment, not to police
which layer a setting "belongs" in. Pushing your whole `.env` is a perfectly
reasonable way to deploy; what is not reasonable is pushing `localhost` into a
cluster and finding out from a 421 an hour later.

Errors (the push is blocked):

* quoted values and trailing whitespace, because
  `kubectl create secret --from-env-file` keeps both verbatim -- a quoted
  `TOKEN_ENCRYPTION_KEY` fails Fernet at boot, and a trailing space on
  `MCP_API_KEY` makes every request 401 with nothing in the logs;
* a required key that is missing or empty;
* a local-only value in a key the cluster has to resolve or advertise.

Warnings (the push proceeds):

* keys the chart also sets. The Secret is listed after the ConfigMap in
  `envFrom`, so these win. That is fine when deliberate, and worth saying out
  loud either way.

    python scripts/validate_env_file.py .env.prod
    python scripts/validate_env_file.py .env --allow-local   # local target
    python scripts/validate_env_file.py .env.prod --strict   # warnings fail too

Prints key names only, never values.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED = [
    "OURA_CLIENT_ID",
    "OURA_CLIENT_SECRET",
    "MCP_API_KEY",
    "TOKEN_ENCRYPTION_KEY",
    "MONGODB_URI",
    "REDIS_URL",
]

OPTIONAL = ["OURA_WEBHOOK_VERIFICATION_TOKEN", "MCP_OWNER_PASSWORD"]

#: Keys whose value the cluster must be able to reach or advertise. A localhost
#: value here is a development leftover that breaks the deployment.
MUST_NOT_BE_LOCAL = {
    "PUBLIC_HOST": "OAuth metadata and the Host allowlist are built from it, so "
                   "clients would be sent to localhost and real requests would 421",
    "OURA_REDIRECT_URI": "Oura redirects the browser here after consent",
    "MONGODB_URI": "the pod cannot reach your machine",
    "REDIS_URL": "the pod cannot reach your machine",
}

LOCAL_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")

#: Settings the chart also renders into the ConfigMap. Present here they simply
#: win, which is a note rather than a fault.
CONFIGMAP_KEYS = {
    "HOST", "PORT", "PUBLIC_HOST", "PUBLIC_SCHEME", "LOG_LEVEL", "MCP_PATH",
    "MCP_STATELESS", "MONGODB_DATABASE", "OURA_ACCOUNT_ID", "OURA_SCOPES",
    "OURA_REDIRECT_URI", "CACHE_ENABLED", "MCP_EXTRA_ALLOWED_HOSTS",
}


def looks_local(value: str) -> bool:
    return any(marker in value.lower() for marker in LOCAL_MARKERS)


def parse(path: Path) -> tuple[dict[str, str], list[str]]:
    """Parse the file the way kubectl does, collecting syntax faults."""
    values: dict[str, str] = {}
    errors: list[str] = []
    seen: dict[str, int] = {}

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            errors.append(f"line {number}: not KEY=VALUE")
            continue

        key, _, value = raw.partition("=")
        key = key.strip()

        if " " in key:
            errors.append(f"line {number}: key {key!r} has whitespace")

        if value != value.rstrip():
            errors.append(
                f"line {number}: {key} has trailing whitespace -- kubectl keeps it, "
                "and it becomes part of the secret"
            )
        if value.startswith(" "):
            errors.append(f"line {number}: {key} has a leading space in its value")

        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            errors.append(
                f"line {number}: {key} is quoted -- kubectl stores the quotes too. "
                "Remove them."
            )

        if key in seen:
            # kubectl and our own parser both take the last assignment, so a
            # duplicate silently wins over the one you were looking at.
            errors.append(
                f"line {number}: {key} is set again here, first at line {seen[key]}. "
                "The last assignment wins silently -- delete one."
            )
        seen[key] = number
        values[key] = stripped

    return values, errors


def check(values: dict[str, str], *, allow_local: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for key in REQUIRED:
        if key not in values:
            errors.append(f"{key} is missing")
        elif not values[key]:
            errors.append(f"{key} is empty")

    if not allow_local:
        for key, why in MUST_NOT_BE_LOCAL.items():
            if values.get(key) and looks_local(values[key]):
                errors.append(
                    f"{key} points at localhost -- {why}. "
                    "Use the cluster value, or pass --allow-local for a local target."
                )

        host = values.get("PUBLIC_HOST", "")
        if values.get("PUBLIC_SCHEME") == "http" and host and not looks_local(host):
            errors.append(
                "PUBLIC_SCHEME is http for a non-local PUBLIC_HOST. OAuth metadata "
                "would advertise http and clients would send tokens in the clear."
            )

    overrides = sorted(set(values) & CONFIGMAP_KEYS)
    if overrides:
        warnings.append(
            "these also come from the chart's ConfigMap, and the Secret is applied "
            "after it, so the values here win: " + ", ".join(overrides)
        )
        warnings.append(
            "that is fine if deliberate. It bites when the file drifts from the "
            "chart -- PUBLIC_HOST and OURA_REDIRECT_URI are the ones that hurt."
        )

    return errors, warnings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", help="env file to check")
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="permit localhost values (pushing to a local or dev target)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as errors"
    )
    args = parser.parse_args(argv[1:])

    path = Path(args.path)
    if not path.is_file():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        print("Copy .env.prod.example to .env.prod and fill it in.", file=sys.stderr)
        return 2

    values, syntax_errors = parse(path)
    errors, warnings = check(values, allow_local=args.allow_local)
    errors = syntax_errors + errors

    for warning in warnings:
        print(f"note: {warning}")

    if errors:
        print(f"\n{path}: {len(errors)} problem(s) -- nothing was pushed\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if warnings and args.strict:
        print(f"\n{path}: warnings, and --strict was given", file=sys.stderr)
        return 1

    present = [k for k in REQUIRED + OPTIONAL if values.get(k)]
    extra = len(values) - len(present)
    summary = f"{len(present)} recognised keys"
    if extra > 0:
        summary += f" plus {extra} other"
    print(f"\n{path}: OK -- {summary}: {', '.join(present)}")

    if not values.get("MCP_OWNER_PASSWORD"):
        print("note: MCP_OWNER_PASSWORD is unset, so MCP OAuth stays off "
              "(ChatGPT will not be able to connect).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
