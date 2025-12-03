#!/usr/bin/env bash
set -euo pipefail

IMG="bitaxe-bench-web:latest"

# use sudo only if you're not in the docker group
SUDO=""
if ! id -nG "$USER" | grep -q '\bdocker\b'; then
  SUDO="sudo"
fi

cd "$(dirname "$0")"

echo "Building $IMG ..."
# add --pull to refresh base image, or --no-cache for a truly clean build
$SUDO docker build -t "$IMG" .

echo "Built image:"
$SUDO docker image inspect "$IMG" --format '{{.Id}}'
