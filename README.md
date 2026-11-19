# fe_sw_at

A personal [bootc](https://containers.github.io/bootc/) image built on
`quay.io/fedora/fedora-sway-atomic:44`. GitHub Actions rebuilds it every day on
top of Fedora's latest base, signs it with cosign, and publishes it to
`ghcr.io/s-gaisser/fe_sw_at`.

## What differs from stock Fedora Sway Atomic

**Added:**
- tailscale (`tailscaled` enabled)
- syncthing (enabled as a per-user service)
- qutebrowser (default browser), with Vivaldi as a fallback
- xplr as the folder handler (built from source)
- kakoune as the system editor
- gh, rclone, fastfetch, ramalama
- tesseract with all language and script packs
- `sway_wp.py`: a wallpaper that explains the session from its own configuration
  (key bindings, window decorations, gaps, bar, notifications, launcher, lock
  screen, nag bar, terminal, outputs, inputs), drawn with sway's own
  Pango/Cairo rules and refreshed on every sway reload. Choose what it shows in
  `/etc/sway_wp/sway_wp-elements.config` (per user:
  `~/.config/sway_wp/sway_wp-elements.config`); `sway_wp.py --list` lists
  every element and its sources

**Removed:**
- Firefox
- VM guest tools, mobile modem support, printing, scanning
- VPN plugins, accessibility tools, CJK/m17n input methods
- firmware for Intel, NVIDIA, Broadcom and other hardware this machine doesn't have
- Thunar, vim and nano
- the Flathub remote and gamemode

The full list is in [`packages.remove`](packages.remove).

**Configured:**
- US keyboard with Compose on right Alt
- chronyd on, fingerprint auth on
- automatic update staging
- firewall: no SSH on the LAN, `tailscale0` trusted

## Layout

| Path | Purpose |
|---|---|
| `Containerfile` | The image definition |
| `packages.install` / `packages.remove` | Package changes, one per line |
| `files/` | Copied to `/` in the image |
| `iso/` | Installer ISO build (`sudo iso/build-netinst-iso.sh`) |
| `backup/` | Previous versions of replaced files (not part of the image) |
| `.github/workflows/build.yml` | Daily build, push and sign |

## Everyday use

```sh
rpm-ostree upgrade        # updates are also staged automatically; reboot to apply
rpm-ostree rollback       # boot the previous image again
bootc status              # shows which image you're on
```

To change something, edit the repo and push. The next build is picked up by
the next update.

## First-time setup (signing)

```sh
podman run --rm -it -v "$PWD:/w:z" -w /w ghcr.io/sigstore/cosign/cosign:latest generate-key-pair
mv cosign.pub files/etc/pki/containers/fe_sw_at.pub
gh secret set SIGNING_SECRET   < cosign.key   # then store cosign.key OFFLINE and delete it here
gh secret set SIGNING_PASSWORD                # the password chosen above
```

After the first successful build, set the ghcr package's visibility to
**public**.

## Installing

1. Run `sudo iso/build-netinst-iso.sh`. It downloads the official Fedora
   netinstall ISO, verifies it and embeds [`iso/fe_sw_at.ks`](iso/fe_sw_at.ks).
   The result is `iso/output-netinst/fe_sw_at-netinst.iso`.
2. Write the ISO to a USB stick and boot from it. Connect to the network
   (Wi-Fi works; the image is pulled during installation), choose the disk
   with **encryption enabled**, and create the user and hostname.
3. After the first boot, run:
   ```sh
   sudo bootc switch --enforce-container-sigpolicy ghcr.io/s-gaisser/fe_sw_at:44
   sudo tailscale up
   ```
4. Restore your home directory from the backup. Personal settings, such as
   the IBus input method environment and `~/.XCompose`, live there and not in
   the image.

**Offline alternative:** `sudo iso/build-iso.sh` builds an ISO with the image
already inside (`iso/output/bootiso/install.iso`), so it needs no network.
Its installer is built by bootc-image-builder from a minimal package set
that lacks AMD GPU and MediaTek Wi-Fi firmware. On this machine the installer
therefore runs at a fallback resolution and shows no Wi-Fi. The installed
system is not affected.
