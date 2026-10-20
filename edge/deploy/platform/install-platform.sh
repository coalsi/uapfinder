#!/usr/bin/env bash
# Platform support for the UAP Finder node on a Jetson Orin Nano developer
# kit with Jetson Linux R36 (JetPack 6): the pps-gpio kernel module and the
# device-tree overlays for the GPS PPS input on 40-pin header pin 7 (PAC.06)
# and the IMX477 camera on CAM0, registered with the bootloader through a
# separate, reversible extlinux.conf entry.
#
#   sudo edge/deploy/platform/install-platform.sh [--chroot]
#
# Called by edge/deploy/install-node.sh (which passes its own arguments
# through), and by the image build if install-node.sh did not call it.
#   --chroot   running inside an image-build chroot (qemu): use the kernel(s)
#              under /lib/modules instead of `uname -r`, do not touch the
#              running hardware; the overlays are switched on at the first
#              boot by uapfinder-overlays.service (which reboots once).
# Without --chroot (a running Jetson) the extlinux entry is written now and
# takes effect at the next reboot.
#
# Environment (all optional):
#   SKYNET_SKIP_APT=1          do not apt-get install anything
#   UAPF_CAMERA_OVERLAY=FILE   overlay in /boot to use for the camera, or "none"
#   UAPF_KVERS="5.15.x-tegra"  kernel version(s) to build the module for
#   UAPF_FORCE_PPS_BUILD=1     build the module even if the kernel has one
#   UAPF_PLATFORM_TEST=1       run on a non-L4T system (tests only)
#
# Idempotent. NOT hardware-tested (see edge/deploy/image/README.md).
set -euo pipefail

log() { printf '\033[1;34m[platform]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[platform] WARNING:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[platform] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CHROOT=0
for a in "$@"; do
    case "$a" in
        --chroot) CHROOT=1 ;;
        -h | --help) sed -n '2,27p' "$0"; exit 0 ;;
        *) log "ignoring argument $a (not used by install-platform.sh)" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/usr/lib/uapfinder/platform
ETC=/etc/uapfinder
PPS_DTBO=/boot/uapfinder-pps-pin7.dtbo
export DEBIAN_FRONTEND=noninteractive

if [[ -f /etc/nv_tegra_release ]]; then
    log "L4T: $(head -n1 /etc/nv_tegra_release)"
    grep -q '^# R36' /etc/nv_tegra_release || warn "written for L4T R36 (JetPack 6); continuing anyway"
    IS_L4T=1
elif [[ "${UAPF_PLATFORM_TEST:-0}" == 1 ]]; then
    warn "not L4T, continuing because UAPF_PLATFORM_TEST=1"
    IS_L4T=0
else
    warn "/etc/nv_tegra_release not found: not Jetson Linux; skipping the Jetson platform setup"
    exit 0
fi

apt_install() {
    if [[ "${SKYNET_SKIP_APT:-0}" == 1 ]]; then
        warn "SKYNET_SKIP_APT=1: not installing $*"
        return 0
    fi
    local missing=() p
    for p in "$@"; do
        dpkg-query -W -f '${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
    done
    [[ ${#missing[@]} -eq 0 ]] && return 0
    log "apt-get install ${missing[*]}"
    if ! apt-get install -y --no-install-recommends "${missing[@]}"; then
        apt-get update && apt-get install -y --no-install-recommends "${missing[@]}"
    fi
}

# dtc/fdtget/fdtoverlay are needed at every boot by configure-overlays.sh.
apt_install device-tree-compiler kmod

# --- 1. pps-gpio kernel module -------------------------------------------------------------
if [[ -n "${UAPF_KVERS:-}" ]]; then
    read -r -a KVERS <<<"$UAPF_KVERS"
elif [[ $CHROOT == 1 ]]; then
    KVERS=()
    for d in /lib/modules/*; do
        [[ -f "$d/modules.builtin" || -d "$d/kernel" ]] && KVERS+=("$(basename "$d")")
    done
else
    KVERS=("$(uname -r)")
fi
[[ ${#KVERS[@]} -gt 0 ]] || warn "no kernel found under /lib/modules"

# builtin | module | missing | nocore
pps_state() {
    local k="$1" mdir="/lib/modules/$1"
    local core=0
    if grep -qE '(^|/)pps_core\.ko' "$mdir/modules.builtin" 2>/dev/null \
        || find "$mdir" -name 'pps_core.ko*' -print -quit 2>/dev/null | grep -q .; then
        core=1
    fi
    if grep -qE '(^|/)pps-gpio\.ko|(^|/)pps_gpio\.ko' "$mdir/modules.builtin" 2>/dev/null; then
        echo builtin
    elif find "$mdir" \( -name 'pps-gpio.ko*' -o -name 'pps_gpio.ko*' \) -print -quit 2>/dev/null | grep -q .; then
        echo module
    elif [[ $core == 1 ]]; then
        echo missing
    else
        echo nocore
    fi
    : "$k"
}

build_pps() {
    local k="$1" hdr="/lib/modules/$1/build" tmp
    if [[ ! -f "$hdr/Makefile" ]]; then
        if [[ $IS_L4T == 1 ]]; then
            apt_install nvidia-l4t-kernel-headers || true
        else
            apt_install "linux-headers-$k" || true
        fi
    fi
    [[ -f "$hdr/Makefile" ]] || die "no kernel headers for $k at $hdr (install nvidia-l4t-kernel-headers)"
    apt_install make gcc libc6-dev
    tmp="$(mktemp -d)"
    cp "$SCRIPT_DIR/pps-gpio/pps-gpio.c" "$SCRIPT_DIR/pps-gpio/Makefile" "$tmp/"
    log "building pps-gpio.ko for $k (out of tree, against $hdr)"
    make -C "$hdr" M="$tmp" -j"$(nproc)" modules >"$tmp/build.log" 2>&1 \
        || { cat "$tmp/build.log" >&2; rm -rf "$tmp"; die "pps-gpio module build failed for $k"; }
    install -D -m 0644 "$tmp/pps-gpio.ko" "/lib/modules/$k/updates/uapfinder/pps-gpio.ko"
    rm -rf "$tmp"
    depmod -a "$k"
    local vm
    vm="$(modinfo -k "$k" -F vermagic pps-gpio 2>/dev/null || true)"
    [[ "$vm" == "$k "* ]] || die "pps-gpio for $k has vermagic '$vm'"
    log "installed /lib/modules/$k/updates/uapfinder/pps-gpio.ko (vermagic: $vm)"
}

PPS_MODE=unavailable
for k in "${KVERS[@]}"; do
    state="$(pps_state "$k")"
    if [[ "${UAPF_FORCE_PPS_BUILD:-0}" == 1 ]]; then state=missing; fi
    case "$state" in
        builtin) log "kernel $k: pps-gpio is built in"; PPS_MODE=builtin ;;
        module) log "kernel $k: pps-gpio module present"; PPS_MODE=module ;;
        missing) build_pps "$k"; PPS_MODE=built ;;
        nocore)
            warn "kernel $k has no PPS support (CONFIG_PPS): GPS PPS cannot work with this kernel;"
            warn "the node falls back to GPS serial time / network time"
            ;;
    esac
done
if [[ $PPS_MODE == module || $PPS_MODE == built ]]; then
    install -d /etc/modules-load.d
    printf '# Written by install-platform.sh: GPS PPS on header pin 7 (device-tree overlay)\npps-gpio\n' \
        >/etc/modules-load.d/uapfinder-pps.conf
fi

# --- 2. device-tree overlays -----------------------------------------------------------------
install -d -m 0755 /boot "$ETC" "$PREFIX"
log "compiling the PPS overlay -> $PPS_DTBO"
dtc -@ -q -I dts -O dtb -o "$PPS_DTBO.tmp" "$SCRIPT_DIR/overlays/uapfinder-pps-pin7.dts"
mv -f "$PPS_DTBO.tmp" "$PPS_DTBO"
chmod 0644 "$PPS_DTBO"

# NVIDIA ships the IMX477 overlays for the Orin Nano devkit carrier (p3768)
# in nvidia-l4t-kernel-dtbs. "-A" is the single camera on CSI port A, which
# is the CAM0 connector; the dual overlay (CAM0 + CAM1) is the fallback.
CAMERA_OVERLAY="${UAPF_CAMERA_OVERLAY:-}"
if [[ -z "$CAMERA_OVERLAY" ]]; then
    for c in tegra234-p3767-camera-p3768-imx477-A.dtbo tegra234-p3767-camera-p3768-imx477-dual.dtbo; do
        if [[ -f "/boot/$c" ]]; then CAMERA_OVERLAY="/boot/$c"; break; fi
    done
fi
if [[ "$CAMERA_OVERLAY" == none ]]; then
    CAMERA_OVERLAY=""
elif [[ -z "$CAMERA_OVERLAY" ]]; then
    warn "no IMX477 overlay found in /boot (nvidia-l4t-kernel-dtbs): the CSI camera will not be configured"
elif [[ "$CAMERA_OVERLAY" != /* ]]; then
    CAMERA_OVERLAY="/boot/$CAMERA_OVERLAY"
fi
[[ -z "$CAMERA_OVERLAY" || -f "$CAMERA_OVERLAY" ]] || die "camera overlay $CAMERA_OVERLAY not found"
[[ -z "$CAMERA_OVERLAY" ]] || log "camera overlay: $CAMERA_OVERLAY"

{
    echo "# Device-tree overlays for the \"uapfinder\" boot entry, one /boot path per line."
    echo "# Written by edge/deploy/platform/install-platform.sh (re-running it rewrites this file)."
    echo "# Applied by uapfinder-overlays.service at boot; each is checked with fdtoverlay first."
    echo "# To boot without any overlay: touch $ETC/overlays.disabled and reboot."
    echo "$PPS_DTBO"
    [[ -z "$CAMERA_OVERLAY" ]] || echo "$CAMERA_OVERLAY"
} >"$ETC/boot-overlays"

install -m 0755 "$SCRIPT_DIR/configure-overlays.sh" "$PREFIX/configure-overlays.sh"
install -m 0644 "$SCRIPT_DIR/uapfinder-overlays.service" /etc/systemd/system/uapfinder-overlays.service
systemctl enable uapfinder-overlays.service >/dev/null 2>&1 \
    || ln -sf /etc/systemd/system/uapfinder-overlays.service \
        /etc/systemd/system/multi-user.target.wants/uapfinder-overlays.service
cat >"$PREFIX/installed.env" <<EOF
# Written by install-platform.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
PPS_MODE=$PPS_MODE
KERNELS="${KVERS[*]}"
PPS_OVERLAY=$PPS_DTBO
CAMERA_OVERLAY=$CAMERA_OVERLAY
EOF
log "pps-gpio: $PPS_MODE; overlays listed in $ETC/boot-overlays; uapfinder-overlays.service enabled"

# --- 3. on a running Jetson: write the boot entry now ---------------------------------------------
if [[ $CHROOT == 0 && $IS_L4T == 1 && -r /proc/device-tree/compatible ]]; then
    "$PREFIX/configure-overlays.sh"
    log "reboot to switch the overlays on (the stock \"primary\" boot entry stays as the fallback)"
fi
