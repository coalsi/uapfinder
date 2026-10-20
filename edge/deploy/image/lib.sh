# shellcheck shell=bash
# Shared helpers for the UAP Finder image build scripts (sourced, not run).

_ts() { date -u '+%H:%M:%S'; }
log() { printf '\033[1;34m[image %s]\033[0m %s\n' "$(_ts)" "$*"; }
warn() { printf '\033[1;33m[image %s] WARNING:\033[0m %s\n' "$(_ts)" "$*" >&2; }
die() { printf '\033[1;31m[image %s] ERROR:\033[0m %s\n' "$(_ts)" "$*" >&2; exit 1; }
step() { printf '\n\033[1;32m==> [%s] %s\033[0m\n' "$(_ts)" "$*"; }

require_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo)"; }

# aarch64 binaries in the rootfs run through binfmt_misc + qemu-user-static.
require_binfmt() {
    [[ "$(uname -m)" == aarch64 ]] && return 0
    if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
        die "no qemu-aarch64 binfmt handler: apt-get install qemu-user-static binfmt-support"
    fi
    grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-aarch64 || die "the qemu-aarch64 binfmt handler is disabled"
}

# --- chroot -----------------------------------------------------------------------------
# chroot_enter ROOTFS: mount the API file systems, give it the host's DNS,
# and block service starts from package scripts. chroot_leave undoes it all
# (safe to call twice; registered as an EXIT trap by the callers).
CHROOT_ROOT=""
chroot_enter() {
    CHROOT_ROOT="$1"
    local r="$CHROOT_ROOT"
    mountpoint -q "$r/proc" || mount -t proc proc "$r/proc"
    mountpoint -q "$r/sys" || mount -t sysfs sys "$r/sys"
    mountpoint -q "$r/dev" || mount --bind /dev "$r/dev"
    mountpoint -q "$r/dev/pts" || mount --bind /dev/pts "$r/dev/pts"
    install -d "$r/run"
    mountpoint -q "$r/run" || mount -t tmpfs -o mode=0755 tmpfs "$r/run"
    if [[ ! -e "$r/etc/resolv.conf.uapfinder-orig" ]]; then
        if [[ -e "$r/etc/resolv.conf" || -L "$r/etc/resolv.conf" ]]; then
            mv "$r/etc/resolv.conf" "$r/etc/resolv.conf.uapfinder-orig"
        else
            touch "$r/etc/resolv.conf.uapfinder-none"
        fi
    fi
    cp -L /etc/resolv.conf "$r/etc/resolv.conf"
    if [[ ! -e "$r/usr/sbin/policy-rc.d" ]]; then
        printf '#!/bin/sh\n# image build: never start services in the chroot\nexit 101\n' >"$r/usr/sbin/policy-rc.d"
        chmod 0755 "$r/usr/sbin/policy-rc.d"
        touch "$r/usr/sbin/policy-rc.d.uapfinder"
    fi
}
chroot_leave() {
    local r="${CHROOT_ROOT:-}"
    [[ -n "$r" ]] || return 0
    local m
    for m in run dev/pts dev sys proc; do
        if mountpoint -q "$r/$m"; then umount "$r/$m" 2>/dev/null || umount -l "$r/$m"; fi
    done
    if [[ -e "$r/etc/resolv.conf.uapfinder-orig" || -L "$r/etc/resolv.conf.uapfinder-orig" ]]; then
        rm -f "$r/etc/resolv.conf"
        mv "$r/etc/resolv.conf.uapfinder-orig" "$r/etc/resolv.conf"
    elif [[ -e "$r/etc/resolv.conf.uapfinder-none" ]]; then
        rm -f "$r/etc/resolv.conf" "$r/etc/resolv.conf.uapfinder-none"
    fi
    if [[ -e "$r/usr/sbin/policy-rc.d.uapfinder" ]]; then
        rm -f "$r/usr/sbin/policy-rc.d" "$r/usr/sbin/policy-rc.d.uapfinder"
    fi
    CHROOT_ROOT=""
}

# in_chroot CMD...: run a command in the chroot with a clean environment
# (plus the proxy variables, so builds behind a proxy work).
in_chroot() {
    local envs=(DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8 HOME=/root
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin)
    local v
    for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY \
        SKYNET_SKIP_APT SKYNET_SKIP_TAILSCALE UAPF_ENROLL_URL UAPF_PLATFORM_TEST UAPF_FORCE_PPS_BUILD \
        UAPF_KVERS UAPF_CAMERA_OVERLAY PIP_CERT; do
        [[ -n "${!v:-}" ]] && envs+=("$v=${!v}")
    done
    chroot "$CHROOT_ROOT" /usr/bin/env -i "${envs[@]}" "$@"
}

# Read a list file: one item per line, '#' comments and blank lines ignored.
read_list() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e '/^$/d' "$1"; }
