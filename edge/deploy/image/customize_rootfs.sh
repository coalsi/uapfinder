#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Customise a Jetson Linux R36 root filesystem into the UAP Finder node
# image, in a chroot (qemu-user-static on an x86_64 host). Called by
# build-image.sh after apply_binaries.sh and l4t_create_default_user.sh;
# can be run on its own.
#
#   sudo customize_rootfs.sh --rootfs DIR --repo DIR --version V --enroll-url URL
#        [--skip-tailscale] [--skip-readsb] [--test]
#
#   --rootfs DIR      Linux_for_Tegra/rootfs (after apply_binaries.sh)
#   --repo DIR        a checkout of the uapfinder repository
#   --version V       the image version (uapfinder-node-V-orin-nano.img.xz)
#   --enroll-url URL  written to /etc/skynet/defaults.yaml as enroll_url
#   --test            the rootfs is a plain Ubuntu 22.04 arm64 base, not L4T
#                     (local tests: skips the NVIDIA checks)
#
# Steps (each idempotent, logged, fatal on error):
#   packages from packages.txt; purge packages-remove.txt; Tailscale from its
#   apt repository (disabled); readsb built from source (disabled, udev-started);
#   the repository copied in and edge/deploy/install-node.sh --chroot run
#   (it installs the node software, users, units and configs, and calls
#   edge/deploy/platform/install-platform.sh; run here if it did not);
#   /etc/skynet/defaults.yaml; /etc/uapfinder-release; multi-user target, SSH
#   off, default user's password locked; caches, SSH host keys and machine-id
#   removed.
#
# Environment: UAPF_INSTALL_NODE=<path relative to the repo> replaces
# edge/deploy/install-node.sh (local tests use tests/stub-install-node.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
# shellcheck source=versions.env
. "$SCRIPT_DIR/versions.env"

ROOTFS="" REPO="" VERSION="" ENROLL_URL=""
SKIP_TAILSCALE=0 SKIP_READSB=0 TEST=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rootfs) ROOTFS="${2:?}"; shift 2 ;;
        --repo) REPO="${2:?}"; shift 2 ;;
        --version) VERSION="${2:?}"; shift 2 ;;
        --enroll-url) ENROLL_URL="${2-}"; shift 2 ;;
        --skip-tailscale) SKIP_TAILSCALE=1; shift ;;
        --skip-readsb) SKIP_READSB=1; shift ;;
        --test) TEST=1; shift ;;
        -h | --help) sed -n '3,31p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_root
[[ -n "$ROOTFS" && -n "$REPO" && -n "$VERSION" ]] || die "--rootfs, --repo and --version are required"
ROOTFS="$(cd "$ROOTFS" && pwd)"
REPO="$(cd "$REPO" && pwd)"
[[ -x "$ROOTFS/bin/bash" && -d "$ROOTFS/etc/apt" ]] || die "$ROOTFS does not look like a root filesystem"
[[ -f "$REPO/edge/pyproject.toml" && -f "$REPO/docs/NODE_APPLIANCE.md" ]] || die "$REPO is not the uapfinder repository"
[[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+_-]*$ ]] || die "bad version '$VERSION'"
if [[ -n "$ENROLL_URL" ]]; then
    [[ "$ENROLL_URL" =~ ^https?://[^[:space:]\"\']+$ ]] || die "enroll URL '$ENROLL_URL' is not an http(s) URL"
fi
if [[ $TEST == 0 ]]; then
    [[ -f "$ROOTFS/etc/nv_tegra_release" ]] || die "$ROOTFS/etc/nv_tegra_release missing: run apply_binaries.sh first"
    compgen -G "$ROOTFS/etc/apt/sources.list.d/nvidia-l4t-apt-source.list" >/dev/null \
        || die "NVIDIA apt source missing in $ROOTFS: run apply_binaries.sh first"
fi
INSTALL_NODE_REL="${UAPF_INSTALL_NODE:-edge/deploy/install-node.sh}"
[[ -f "$REPO/$INSTALL_NODE_REL" ]] \
    || die "$INSTALL_NODE_REL not found in $REPO (the node installer is required; UAPF_INSTALL_NODE overrides it for tests)"
require_binfmt

trap chroot_leave EXIT
chroot_enter "$ROOTFS"
SRC_IN=/tmp/uapfinder-src

# --- repository ---------------------------------------------------------------------------
step "copy the repository to $SRC_IN in the rootfs"
rm -rf "${ROOTFS:?}$SRC_IN"
install -d "$ROOTFS$SRC_IN"
tar -C "$REPO" --exclude=.git --exclude=node_modules --exclude=.venv --exclude='*/.venv' \
    --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache --exclude=.next \
    --exclude='*.img' --exclude='*.img.xz' -cf - . | tar -C "$ROOTFS$SRC_IN" -xf -
IMG_IN="$SRC_IN/edge/deploy/image"

# --- packages -------------------------------------------------------------------------------
step "apt: update and install packages.txt"
in_chroot apt-get update
in_chroot apt-get -y -f install
mapfile -t PKGS < <(read_list "$SCRIPT_DIR/packages.txt")
in_chroot apt-get install -y --no-install-recommends "${PKGS[@]}"

step "purge desktop and unwanted packages (packages-remove.txt)"
# shellcheck disable=SC2016  # dpkg-query format, not shell
mapfile -t INSTALLED < <(in_chroot dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' | awk '$1 ~ /^.i/ {print $2}')
PURGE=()
while IFS= read -r pat; do
    for p in "${INSTALLED[@]}"; do
        # shellcheck disable=SC2053  # glob match on purpose
        [[ "$p" == $pat ]] && PURGE+=("$p")
    done
done < <(read_list "$SCRIPT_DIR/packages-remove.txt")
if [[ ${#PURGE[@]} -gt 0 ]]; then
    SAFE=()
    for p in "${PURGE[@]}"; do
        if in_chroot apt-get -s purge "$p" | grep -E '^(Remv|Purg) nvidia-l4t-' >/dev/null; then
            warn "keeping $p: removing it would remove NVIDIA L4T packages"
        else
            SAFE+=("$p")
        fi
    done
    if [[ ${#SAFE[@]} -gt 0 ]]; then
        log "purging: ${SAFE[*]}"
        in_chroot apt-get purge -y "${SAFE[@]}"
    fi
else
    log "nothing to purge"
fi

if [[ $SKIP_TAILSCALE == 0 ]]; then
    step "Tailscale (official apt repository, disabled until enrollment)"
    in_chroot bash "$IMG_IN/install-tailscale.sh"
else
    warn "skipping Tailscale (--skip-tailscale)"
fi

if [[ $SKIP_READSB == 0 ]]; then
    step "readsb $READSB_TAG (built from source, disabled until a receiver is present)"
    # shellcheck disable=SC2016  # dpkg-query format, not shell
    if ! in_chroot dpkg-query -W -f '${Version}' readsb 2>/dev/null | grep -q "^${READSB_TAG#v}"; then
        rm -rf "$ROOTFS/tmp/readsb-src"
        git clone --quiet --depth 1 --branch "$READSB_TAG" "$READSB_REPO" "$ROOTFS/tmp/readsb-src"
        got="$(git -C "$ROOTFS/tmp/readsb-src" rev-parse HEAD)"
        [[ "$got" == "$READSB_COMMIT" ]] || die "readsb $READSB_TAG is commit $got, expected $READSB_COMMIT"
        rm -rf "$ROOTFS/tmp/readsb-src/.git"
    fi
    in_chroot bash "$IMG_IN/install-readsb.sh" --src /tmp/readsb-src
    rm -rf "$ROOTFS/tmp/readsb-src"
else
    warn "skipping readsb (--skip-readsb)"
fi

# --- the node software --------------------------------------------------------------------------
step "node software: $INSTALL_NODE_REL --chroot"
export UAPF_ENROLL_URL="$ENROLL_URL"
[[ $SKIP_TAILSCALE == 0 ]] || export SKYNET_SKIP_TAILSCALE=1
in_chroot bash "$SRC_IN/$INSTALL_NODE_REL" --chroot
if [[ ! -x "$ROOTFS/usr/lib/uapfinder/platform/configure-overlays.sh" ]]; then
    step "platform: install-platform.sh --chroot (install-node.sh did not run it)"
    in_chroot bash "$SRC_IN/edge/deploy/platform/install-platform.sh" --chroot
fi

# --- image defaults ---------------------------------------------------------------------------------
step "/etc/skynet/defaults.yaml"
install -d -m 0755 "$ROOTFS/etc/skynet"
DEFAULTS="$ROOTFS/etc/skynet/defaults.yaml"
if [[ -z "$ENROLL_URL" ]]; then
    warn "no enroll URL given: enroll_url stays as install-node.sh wrote it (if at all)"
elif [[ -f "$DEFAULTS" ]] && grep -qE '^enroll_url:' "$DEFAULTS"; then
    sed -i -E "s#^enroll_url:.*#enroll_url: \"${ENROLL_URL}\"#" "$DEFAULTS"
else
    if [[ ! -f "$DEFAULTS" ]]; then
        printf '# Image defaults (docs/NODE_APPLIANCE.md). The setup page'"'"'s advanced section can change them.\n' >"$DEFAULTS"
    fi
    printf 'enroll_url: "%s"\n' "$ENROLL_URL" >>"$DEFAULTS"
fi
chmod 0644 "$DEFAULTS"
grep -E '^enroll_url:' "$DEFAULTS" || warn "defaults.yaml has no enroll_url"

GIT_REV="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
cat >"$ROOTFS/etc/uapfinder-release" <<EOF
# UAP Finder node image (edge/deploy/image/build-image.sh)
UAPF_IMAGE_VERSION=$VERSION
UAPF_IMAGE_BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
UAPF_GIT_COMMIT=$GIT_REV
L4T_VERSION=$L4T_VERSION
JETPACK_VERSION=$JETPACK_VERSION
EOF

# --- system settings --------------------------------------------------------------------------------
step "system settings: headless target, SSH off until the device password is set"
in_chroot systemctl set-default multi-user.target
for u in ssh.service ssh.socket; do in_chroot systemctl disable "$u" >/dev/null 2>&1 || true; done
# The image carries no SSH host keys; make sure they exist whenever sshd starts.
install -d "$ROOTFS/etc/systemd/system/ssh.service.d"
cat >"$ROOTFS/etc/systemd/system/ssh.service.d/10-uapfinder-hostkeys.conf" <<'EOF'
# Written by edge/deploy/image/customize_rootfs.sh: the image has no SSH host
# keys (each card gets its own); create any that are missing before sshd starts.
[Service]
ExecStartPre=/usr/bin/ssh-keygen -A
EOF
# NVIDIA's own first-boot root-partition resize (nvresizefs, R36) and the
# node's first-boot resize must not run at the same time: order ours after.
install -d "$ROOTFS/etc/systemd/system/uapfinder-firstboot.service.d"
cat >"$ROOTFS/etc/systemd/system/uapfinder-firstboot.service.d/10-after-nvidia-firstboot.conf" <<'EOF'
# Written by edge/deploy/image/customize_rootfs.sh. NVIDIA's L4T first-boot
# units (root file system resize, configuration) finish before the node's
# first-boot service grows the partition again (a no-op if already grown).
# Units that do not exist are ignored by systemd.
[Unit]
After=nvresizefs.service nv-oem-config.service nvfb-early.service nvfb.service uapfinder-overlays.service
EOF

if in_chroot getent passwd "$DEFAULT_USER" >/dev/null; then
    in_chroot passwd -l "$DEFAULT_USER" >/dev/null
    log "default account $DEFAULT_USER: password locked (set from the setup page)"
else
    die "default account $DEFAULT_USER does not exist (l4t_create_default_user.sh should have created it)"
fi

# --- clean up ---------------------------------------------------------------------------------------
step "clean up: build dependencies, caches, host keys, machine-id"
in_chroot apt-get -y autoremove --purge
in_chroot apt-get clean
rm -rf "${ROOTFS:?}$SRC_IN"
rm -rf "$ROOTFS"/var/lib/apt/lists/* "$ROOTFS"/var/cache/apt/*.bin "$ROOTFS"/var/cache/debconf/*-old \
    "$ROOTFS"/root/.cache "$ROOTFS"/root/.bash_history "$ROOTFS"/tmp/* "$ROOTFS"/var/tmp/*
find "$ROOTFS/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
find "$ROOTFS/var/log" -type f -exec truncate -s 0 {} +
# Documentation except the copyright files (licences stay with the binaries).
find "$ROOTFS/usr/share/doc" -mindepth 2 ! -name copyright ! -type d -delete 2>/dev/null || true
find "$ROOTFS/usr/share/doc" -mindepth 1 -type d -empty -delete 2>/dev/null || true
rm -f "$ROOTFS"/etc/ssh/ssh_host_*
: >"$ROOTFS/etc/machine-id"
rm -f "$ROOTFS/var/lib/dbus/machine-id"
ln -sf /etc/machine-id "$ROOTFS/var/lib/dbus/machine-id" 2>/dev/null || true
for q in "$ROOTFS"/usr/bin/qemu-aarch64-static "$ROOTFS"/usr/bin/qemu-aarch64; do
    if [[ -e "$q" ]] && ! in_chroot dpkg -S "${q#"$ROOTFS"}" >/dev/null 2>&1; then rm -f "$q"; fi
done

chroot_leave
trap - EXIT
log "rootfs customised: $(du -sh --exclude=proc --exclude=sys "$ROOTFS" 2>/dev/null | cut -f1) in $ROOTFS"
