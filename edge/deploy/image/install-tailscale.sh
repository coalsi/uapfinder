#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Install Tailscale from its official apt repository for Ubuntu 22.04
# (https://pkgs.tailscale.com/stable/#ubuntu-jammy) and leave tailscaled
# disabled: the node enables it and joins the tailnet only when the
# enrollment response carries a Tailscale auth key (docs/NODE_APPLIANCE.md).
#
#   sudo install-tailscale.sh     (on the node, or inside the image chroot)
#
# Idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
# shellcheck source=versions.env
. "$SCRIPT_DIR/versions.env"
require_root
export DEBIAN_FRONTEND=noninteractive

KEYRING=/usr/share/keyrings/tailscale-archive-keyring.gpg
LIST=/etc/apt/sources.list.d/tailscale.list
if ! command -v tailscale >/dev/null; then
    log "adding the Tailscale apt repository"
    tmp="$(mktemp -d)"
    curl -fsSL --retry 3 -o "$tmp/key.gpg" "$TAILSCALE_KEY_URL"
    curl -fsSL --retry 3 -o "$tmp/tailscale.list" "$TAILSCALE_LIST_URL"
    grep -q '^deb .*pkgs\.tailscale\.com' "$tmp/tailscale.list" || die "unexpected content in $TAILSCALE_LIST_URL"
    install -m 0644 "$tmp/key.gpg" "$KEYRING"
    install -m 0644 "$tmp/tailscale.list" "$LIST"
    rm -rf "$tmp"
    apt-get update
    apt-get install -y --no-install-recommends tailscale
fi
systemctl disable tailscaled.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/multi-user.target.wants/tailscaled.service
log "tailscale $(dpkg-query -W -f '${Version}' tailscale 2>/dev/null || echo '?') installed, tailscaled disabled until enrollment"
