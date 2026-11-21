#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" != 0 ]]; then
    echo "Run server preparation as root." >&2
    exit 1
fi

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo "This installer supports Ubuntu 24.04." >&2
    exit 1
fi

if ! id deploy >/dev/null 2>&1; then
    echo "Create the deploy account with bootstrap_access.py first." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

apt-get update
apt-get install -y ca-certificates curl git python3 age

install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location --retry 3 \
    https://download.docker.com/linux/ubuntu/gpg \
    --output /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

setup_arch="$(dpkg --print-architecture)"
cat > /etc/apt/sources.list.d/docker.sources <<DOCKER_REPOSITORY
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $setup_arch
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPOSITORY

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod --append --groups docker deploy

sops_version=3.13.3
if [[ "$setup_arch" != amd64 && "$setup_arch" != arm64 ]]; then
    echo "Unsupported SOPS architecture: $setup_arch" >&2
    exit 1
fi

setup_directory="$(mktemp -d)"
trap 'rm -rf -- "$setup_directory"' EXIT
sops_binary="sops-v$sops_version.linux.$setup_arch"
sops_release="https://github.com/getsops/sops/releases/download/v$sops_version"
curl --fail --silent --show-error --location --retry 3 \
    "$sops_release/$sops_binary" --output "$setup_directory/$sops_binary"
curl --fail --silent --show-error --location --retry 3 \
    "$sops_release/sops-v$sops_version.checksums.txt" --output "$setup_directory/checksums.txt"
(
    cd "$setup_directory"
    sha256sum --check --ignore-missing checksums.txt
)
install -m 0755 "$setup_directory/$sops_binary" /usr/local/bin/sops

echo "Server preparation completed: Docker, Compose, Buildx, Git, Python, age and SOPS."
echo "The deploy account can use Docker in new SSH sessions."
echo "No application containers have been started."
