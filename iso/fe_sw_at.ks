# Kickstart embedded into the official Fedora Everything netinstall ISO by
# build-netinst-iso.sh. That installer ships all firmware (GPU, Wi-Fi), so it
# runs at native resolution and can use Wi-Fi to pull the image.
#
# Language, keyboard and time zone are preset; network, disk (enable
# encryption!), user, password and hostname are chosen interactively.
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts='us'
timezone Europe/Berlin --utc

# Install the image straight from the registry. The installer cannot check
# the cosign signature; after the first boot, `bootc switch
# --enforce-container-sigpolicy` (see README) turns verification on.
ostreecontainer --url=ghcr.io/s-gaisser/fe_sw_at:44 --transport=registry --no-signature-verification
