# Custom Fedora Sway Atomic image.
# Stock fedora-sway-atomic minus unused hardware/services, plus Tailscale,
# Syncthing, qutebrowser (+ Vivaldi fallback), xplr, kakoune and OCR.

# --- Build stage: xplr (not packaged in Fedora) -------------------------------
FROM registry.fedoraproject.org/fedora:44 AS xplr
ARG XPLR_VERSION=1.2.2
RUN dnf -y install cargo gcc && \
    cargo install --locked xplr --version "${XPLR_VERSION}" --root /out

# --- Image --------------------------------------------------------------------
FROM quay.io/fedora/fedora-sway-atomic:44

COPY --from=xplr /out/bin/xplr /usr/bin/xplr
COPY files/ /
COPY packages.remove packages.install /tmp/

# Remove unused stock packages, then install additions from Fedora repos.
RUN set -euf; \
    list() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$'; }; \
    remove=""; for p in $(list /tmp/packages.remove); do if rpm -q "$p" >/dev/null; then remove="$remove $p"; fi; done; \
    dnf -y remove $remove; \
    dnf -y install $(list /tmp/packages.install); \
    dnf clean all

# Vivaldi (fallback browser). /opt is a symlink to /var/opt on Atomic, which is
# not part of the image, so install into a real /opt and move it under /usr/lib.
# tmpfiles.d/vivaldi.conf links /var/opt/vivaldi back to it at boot.
RUN set -eu; \
    rpm --import https://repo.vivaldi.com/archive/linux_signing_key.pub; \
    curl -fsSLo /etc/yum.repos.d/vivaldi.repo https://repo.vivaldi.com/archive/vivaldi-fedora.repo; \
    rm /opt && mkdir /opt; \
    dnf -y install vivaldi-stable; \
    mv /opt/vivaldi /usr/lib/vivaldi; \
    rmdir /opt && ln -s var/opt /opt; \
    dnf clean all

# Services and auth.
RUN set -eu; \
    authselect enable-feature with-fingerprint; \
    systemctl enable tailscaled.service chronyd.service rpm-ostreed-automatic.timer; \
    systemctl --global enable syncthing.service; \
    systemctl mask mdmonitor.service raid-check.timer; \
    rm -rf /tmp/* /run/dnf /var/cache/* /var/log/* /var/lib/dnf /var/opt/*

RUN bootc container lint
