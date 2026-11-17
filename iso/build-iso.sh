#!/usr/bin/env bash
# Build the installer ISO from the published image. Run on the HOST as root:
#   sudo ./iso/build-iso.sh [image-ref]
# Result: iso/output/bootiso/install.iso
set -euo pipefail

IMAGE="${1:-ghcr.io/s-gaisser/fe_sw_at:44}"
BUILDER="quay.io/centos-bootc/bootc-image-builder:latest"
cd "$(dirname "$(readlink -f "$0")")"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (sudo $0)" >&2
    exit 1
fi

mkdir -p output
podman pull "$IMAGE"
podman run --rm -it --privileged --pull=newer \
    --security-opt label=type:unconfined_t \
    -v "$PWD/config.toml:/config.toml:ro" \
    -v "$PWD/output:/output" \
    -v /var/lib/containers/storage:/var/lib/containers/storage \
    "$BUILDER" \
    --type anaconda-iso --rootfs btrfs --config /config.toml \
    "$IMAGE"

echo "ISO: $PWD/output/bootiso/install.iso"
