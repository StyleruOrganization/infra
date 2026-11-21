"""Read an immutable Git revision without checking out local secret files."""

import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit


def git(*args, binary=False):
    try:
        result = subprocess.run(
            ["git", *map(str, args)], check=True, capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=300,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("Git operation failed; confirm repository access and the requested ref") from None
    return result.stdout if binary else result.stdout.decode().strip()


def validate_ref(ref):
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ref.startswith("-"):
        raise ValueError("Use a branch, tag or full commit SHA without shell characters")


def validate_repository(repository):
    url = urlsplit(repository)
    https = url.scheme == "https" and url.hostname and url.username is None and url.password is None
    ssh = re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", repository)
    if not (https or ssh):
        raise ValueError("Use an HTTPS Git URL without credentials or git@host:owner/repository.git")


class Source:
    def __init__(self, root, repository, ref):
        self.root, self.repository, self.ref = Path(root), repository, ref

    def __enter__(self):
        validate_repository(self.repository)
        validate_ref(self.ref)
        cache = self.root / "repositories"
        sources = self.root / "sources"
        cache.mkdir(parents=True, exist_ok=True)
        sources.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(self.repository.encode()).hexdigest()
        mirror = cache / f"{key}.git"
        if not mirror.exists():
            git("clone", "--bare", "--", self.repository, mirror)
        git("--git-dir", mirror, "fetch", "--prune", "origin", self.ref)
        self.sha = git("--git-dir", mirror, "rev-parse", "FETCH_HEAD^{commit}")
        self.temporary = tempfile.TemporaryDirectory(prefix="source-", dir=sources)
        self.path = Path(self.temporary.name)
        try:
            archive = git("--git-dir", mirror, "archive", self.sha, binary=True)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                members = [
                    item for item in bundle.getmembers()
                    if ".cursorignore" not in Path(item.name).parts
                    and Path(item.name).name != ".env"
                ]
                bundle.extractall(self.path, members=members, filter="data")
        except BaseException:
            self.temporary.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        self.temporary.cleanup()
