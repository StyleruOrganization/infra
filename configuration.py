"""Inventory and secret rendering; no Docker or network calls at import time."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPOSITORY = Path(__file__).resolve().parent


def slug(value):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ValueError("Names may contain lowercase letters, numbers and single dashes")
    return value


def under(base, relative):
    base = Path(base).resolve()
    if ".cursorignore" in Path(relative).parts:
        raise ValueError(".cursorignore files must not be modified")
    path = (base / relative).resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError("Paths must remain inside their owning directory")
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def project(name):
    return read_json(REPOSITORY / "projects" / slug(name) / "project.json")


def host(name):
    value = read_json(REPOSITORY / "hosts" / slug(name) / "host.json")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value["ssh_secrets_prefix"]):
        raise ValueError("Invalid SSH secret prefix")
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", value["root"]):
        raise ValueError("Server root must be an absolute path without spaces")
    return value


def projects_on_host(name):
    entries = [(path.parent.name, read_json(path)) for path in sorted((REPOSITORY / "projects").glob("*/project.json"))]
    return [(key, value) for key, value in entries if value["host"] == name]


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value, mode=0o644):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode)


def decrypt(path, root):
    if not path.is_file():
        raise RuntimeError(f"Initialize and fill {path.relative_to(REPOSITORY)} with tools/manage_secrets.py first")
    result = subprocess.run(
        ["sops", "--decrypt", "--output-type", "json", str(path)],
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(Path(root) / "keys" / "age.key")},
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"SOPS could not decrypt {path.relative_to(REPOSITORY)}; check the server age key")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("Decrypted secret file must contain a JSON object") from None
    if not isinstance(value, dict):
        raise RuntimeError("Decrypted secret file must contain a JSON object")
    return value


def resolve(value, secrets):
    if isinstance(value, dict):
        if set(value) != {"secret"}:
            raise ValueError("Secret references must have exactly one 'secret' field")
        name = value["secret"]
        if name not in secrets or secrets[name] is None or secrets[name] == "":
            raise RuntimeError(f"Fill secret {name} before deployment")
        return secrets[name]
    return value


def env_text(values):
    lines = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"Invalid environment variable name: {name}")
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            value = str(value)
        if "\n" in value or "\r" in value or "\0" in value or value != value.strip():
            raise ValueError(f"{name} cannot contain line breaks or leading/trailing whitespace")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def prepare_project(name, definition, root):
    directory = Path(root) / "projects" / name
    secrets_path = REPOSITORY / "projects" / name / "secrets.enc.json"
    needs_secrets = bool(definition.get("environment") or definition.get("files") or definition.get("alerts"))
    secrets = decrypt(secrets_path, root) if needs_secrets or secrets_path.exists() else {}
    environments = {
        group: env_text({key: resolve(value, secrets) for key, value in values.items()})
        for group, values in definition.get("environment", {}).items()
    }
    files = {path: resolve(value, secrets) for path, value in definition.get("files", {}).items()}
    for path, value in files.items():
        under(directory, path)
        if not isinstance(value, str):
            raise ValueError(f"{path} must resolve to a string")
    directory.mkdir(parents=True, exist_ok=True)
    for name_, content in environments.items():
        slug(name_)
        env_dir = directory / "env"
        env_dir.mkdir(exist_ok=True, mode=0o700)
        atomic_write(env_dir / f"{name_}.env", content, 0o600)
    for path, content in files.items():
        destination = under(directory, path)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(destination, content, 0o600)
    return directory, secrets
