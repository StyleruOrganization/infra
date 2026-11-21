#!/usr/bin/env python3
"""Create encrypted settings once; edit them with your ordinary SOPS editor."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "edit"))
    parser.add_argument("scope", choices=("host", "project"))
    parser.add_argument("name")
    parser.add_argument("--recipient", help="Comma-separated owner/server age public keys; needed for init")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise ValueError("Invalid inventory name")
    directory = ROOT / ("hosts" if args.scope == "host" else "projects") / args.name
    path = directory / "secrets.enc.json"
    if args.action == "edit":
        if not path.is_file():
            raise RuntimeError("Run init for this inventory entry first")
        environment = os.environ.copy()
        if not any(environment.get(name) for name in ("SOPS_EDITOR", "EDITOR", "VISUAL")):
            if shutil.which("code"):
                environment["EDITOR"] = "code --wait"
            elif shutil.which("nano"):
                environment["EDITOR"] = "nano"
        subprocess.run(["sops", str(path)], env=environment, check=True)
        return
    if path.exists():
        raise RuntimeError("Encrypted settings already exist; use edit to preserve existing passwords and keys")
    if not args.recipient:
        raise ValueError("Pass --recipient with the owner and server age public keys")
    if not all(re.fullmatch(r"age1[0-9a-z]+", key) for key in args.recipient.split(",")):
        raise ValueError("Use comma-separated age public keys without spaces")
    template = json.loads((directory / "secrets.example.json").read_text())
    values = template["values"]
    for name in template.get("generate", []):
        values[name] = secrets.token_hex(32)
    if template.get("rsa"):
        private = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072"],
            check=True, capture_output=True,
        ).stdout
        public = subprocess.run(
            ["openssl", "pkey", "-pubout"], input=private, check=True, capture_output=True,
        ).stdout
        values[template["rsa"]["private"]] = private.decode()
        values[template["rsa"]["public"]] = public.decode()
    result = subprocess.run(
        ["sops", "--encrypt", "--age", args.recipient, "--input-type", "json", "--output-type", "json", "/dev/stdin"],
        input=json.dumps(values).encode(), capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("SOPS encryption failed; confirm the installed sops version and age recipients")
    with path.open("xb") as stream:
        stream.write(result.stdout)
    print(f"Created {path.relative_to(ROOT)}. Use edit to fill external credentials.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Secret setup stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from None
