#!/usr/bin/env python3
"""Prepare a read-only GitHub deploy key; add its public half in GitHub."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

GITHUB_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def write_private(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias", default="github-infra")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.alias):
        raise ValueError("Use a lowercase SSH alias")
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    key = directory / f"{args.alias}-read"
    public = key.with_suffix(".pub")
    if key.exists() != public.exists():
        raise RuntimeError("Incomplete key pair; existing files were preserved")
    if not key.exists():
        subprocess.run([
            "ssh-keygen", "-t", "ed25519", "-N", "", "-C",
            f"platform-{args.alias}-read", "-f", str(key),
        ], check=True, capture_output=True)
    key.chmod(0o600)
    public.chmod(0o644)
    known = directory / "known_hosts"
    content = known.read_text() if known.exists() else ""
    if GITHUB_KEY not in content.splitlines():
        content = content.rstrip("\n") + "\n" + GITHUB_KEY + "\n"
        write_private(known, content.lstrip("\n"))
    config = directory / "config"
    content = config.read_text() if config.exists() else ""
    if not any(line.strip() == f"Host {args.alias}" for line in content.splitlines()):
        entry = (
            f"Host {args.alias}\n"
            "    HostName ssh.github.com\n"
            "    Port 443\n"
            "    User git\n"
            f"    IdentityFile {key}\n"
            "    IdentitiesOnly yes\n"
            "    ForwardAgent no\n"
            "    StrictHostKeyChecking yes\n"
            "    HostKeyAlias github.com\n\n"
        )
        write_private(config, entry + content)
    print(public.read_text().strip())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Git access setup stopped: {error}") from None
