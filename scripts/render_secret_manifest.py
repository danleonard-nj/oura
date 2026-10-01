#!/usr/bin/env python3
"""Render an env file as a Kubernetes Secret manifest.

The env file has already been validated by validate_env_file.py before this
runs. Reusing the same parser keeps duplicate keys deterministic: the last
assignment wins, matching the validation summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from validate_env_file import parse


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", help="env file to render")
    parser.add_argument("name", help="Secret name")
    parser.add_argument("namespace", help="Kubernetes namespace")
    args = parser.parse_args(argv[1:])

    values, errors = parse(Path(args.path))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": args.name, "namespace": args.namespace},
        "type": "Opaque",
        "stringData": values,
    }
    json.dump(manifest, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
