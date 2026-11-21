#!/usr/bin/env python3
"""Installed bootstrap: execute the exact infrastructure revision selected by Actions."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from source import Source, validate_ref


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    commands = parser.add_subparsers(dest="action", required=True)
    release = commands.add_parser("release")
    release.add_argument("project")
    release.add_argument("service")
    release.add_argument("ref")
    infrastructure = commands.add_parser("infrastructure")
    infrastructure.add_argument("project")
    infrastructure.add_argument("--update-host", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("An exact infrastructure commit SHA is required")
    validate_ref(args.project)
    if args.action == "release":
        validate_ref(args.service)
        validate_ref(args.ref)
    repository = json.loads((root / "bootstrap.json").read_text())["repository"]
    with Source(root, repository, args.revision) as source:
        if source.sha != args.revision:
            raise RuntimeError("Fetched infrastructure revision differs from the requested commit")
        command = [
            sys.executable, str(source.path / "deploy.py"), "--root", str(root),
            "--revision", source.sha, args.action, args.project,
        ]
        if args.action == "release":
            command += [args.service, args.ref]
        elif args.update_host:
            command += ["--update-host"]
        subprocess.run(command, check=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Deployment stopped: {error}") from None
