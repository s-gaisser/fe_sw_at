#!/usr/bin/env bash
# Build the installer ISO: the official Fedora Everything netinstall ISO with
# fe_sw_at.ks embedded. Run on the HOST as root: mkksiso needs loop devices
# to rebuild the EFI boot image, hence --privileged and the host's /dev.
#   sudo ./iso/build-netinst-iso.sh
# Result: iso/output-netinst/fe_sw_at-netinst.iso
#
# The Fedora ISO is downloaded once, checked against Fedora's GPG-signed
# CHECKSUM file and cached in iso/output-netinst/.
set -euo pipefail

FEDORA=44
cd "$(dirname "$(readlink -f "$0")")"
if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (sudo $0)" >&2
    exit 1
fi

mkdir -p output-netinst

podman run --rm --privileged --security-opt label=disable -v /dev:/dev \
    -v "$PWD:/iso" -w /iso/output-netinst -e FEDORA="$FEDORA" \
    "registry.fedoraproject.org/fedora:$FEDORA" bash -euo pipefail -c '
        dnf -y -q install lorax gnupg2 >/dev/null 2>&1
        base="https://download.fedoraproject.org/pub/fedora/linux/releases/$FEDORA/Everything/x86_64/iso"

        checksum=$(curl -fsSL "$base/" | grep -oE "Fedora-Everything-[0-9.-]+-x86_64-CHECKSUM" | head -n1)
        curl -fsSLo "$checksum" "$base/$checksum"
        gpg --batch --quiet --dearmor -o /tmp/fedora.gpg \
            < "/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$FEDORA-primary"
        gpgv --keyring /tmp/fedora.gpg "$checksum"

        iso=$(grep -oE "Fedora-Everything-netinst-x86_64-[0-9.-]+\.iso" "$checksum" | head -n1)
        if ! grep -F "($iso)" "$checksum" | sha256sum --status -c -; then
            curl -fLo "$iso" "$base/$iso"
            grep -F "($iso)" "$checksum" | sha256sum -c -
        fi

        rm -f fe_sw_at-netinst.iso
        mkksiso --ks /iso/fe_sw_at.ks "$iso" fe_sw_at-netinst.iso
    '

# Hand the results back to the user who ran sudo.
if [ -n "${SUDO_UID:-}" ]; then
    chown -R "$SUDO_UID:$SUDO_GID" output-netinst
fi

echo "ISO: $PWD/output-netinst/fe_sw_at-netinst.iso"
