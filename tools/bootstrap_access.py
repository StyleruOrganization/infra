#!/usr/bin/env python3
"""Create a deployment account and install public SSH keys on a Linux host."""

import argparse
import base64
import grp
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile


def public_key(value):
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("Use a single Ed25519 public key")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid SSH public key encoding") from None
    if len(decoded) != 51 or decoded[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise argparse.ArgumentTypeError("Invalid Ed25519 public key")
    return " ".join(parts)


def install_keys(account, keys):
    directory = Path(account.pw_dir) / ".ssh"
    destination = directory / "authorized_keys"
    if directory.is_symlink() or destination.is_symlink():
        raise RuntimeError("SSH paths must not be symlinks")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    os.chown(directory, account.pw_uid, account.pw_gid)
    content = destination.read_text() if destination.exists() else ""
    existing = {
        tuple(parts[:2])
        for line in content.splitlines()
        if (parts := line.strip().split()) and parts[0] == "ssh-ed25519"
    }
    for key in keys:
        identity = tuple(key.split()[:2])
        if identity not in existing:
            content = content.rstrip("\n") + "\n" + key + "\n"
            existing.add(identity)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, mode="w", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
            stream.write(content.lstrip("\n"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="deploy")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--admin-key", type=public_key, required=True)
    parser.add_argument("--actions-key", type=public_key, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run the initial account setup as root")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", args.user) or args.user == "root":
        raise ValueError("Choose a regular deployment account")
    if not args.root.is_absolute() or len(args.root.parts) < 3 or args.root.is_symlink():
        raise ValueError("Use a dedicated absolute installation directory")
    try:
        account = pwd.getpwnam(args.user)
    except KeyError:
        subprocess.run([
            "useradd", "--create-home", "--user-group", "--shell", "/bin/bash",
            "--comment", "Platform deployment", args.user,
        ], check=True)
        account = pwd.getpwnam(args.user)
    if account.pw_uid < 1000 or account.pw_shell != "/bin/bash":
        raise RuntimeError("The existing account is not a regular deployment user")
    install_keys(account, [args.admin_key, args.actions_key])
    install_keys(pwd.getpwnam("root"), [args.admin_key])
    try:
        grp.getgrnam("docker")
    except KeyError:
        pass
    else:
        subprocess.run(["usermod", "--append", "--groups", "docker", args.user], check=True)
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(args.root, account.pw_uid, account.pw_gid)
    keys = args.root / "keys"
    if keys.exists():
        if keys.is_symlink() or (keys / "age.key").is_symlink():
            raise RuntimeError("Age key paths must not be symlinks")
        os.chown(keys, account.pw_uid, account.pw_gid)
        keys.chmod(0o700)
        key = keys / "age.key"
        if key.exists():
            os.chown(key, account.pw_uid, account.pw_gid)
            key.chmod(0o600)
    print(f"Configured {args.user}: personal and Actions SSH access, installation directory {args.root}.")
    print("Root personal key installed; SSH password settings and existing authorized keys preserved.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Access setup stopped: {error}") from None
