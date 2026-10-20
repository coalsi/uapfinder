#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Local test of the image customisation without NVIDIA's BSP: runs
# customize_rootfs.sh on a plain Ubuntu 22.04 arm64 base root file system in
# a qemu chroot (with tests/stub-install-node.sh unless the real
# edge/deploy/install-node.sh is asked for), builds the pps-gpio module
# against Ubuntu's 5.15 arm64 headers, then packs the result into a GPT disk
# image with an APP partition and runs test-image.sh on the rootfs, the
# .img and the .img.xz.
#
#   sudo tests/local-chroot-test.sh ubuntu-base-22.04.5-base-arm64.tar.gz WORKDIR [--real-install-node] [--skip-readsb]
#
# Needs: qemu-user-static with the qemu-aarch64 binfmt handler registered,
# sgdisk, mkfs.ext4 (-d), xz, git. Behind an HTTPS proxy (HTTPS_PROXY), apt
# in the chroot is pointed at it with https://ports.ubuntu.com and the CA in
# SSL_CERT_FILE / /root/.ccr/ca-bundle.crt (test setup only; removed again).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="$(cd "$IMAGE_DIR/../../.." && pwd)"
# shellcheck source=../lib.sh
. "$IMAGE_DIR/lib.sh"
# shellcheck source=../versions.env
. "$IMAGE_DIR/versions.env"

[[ $# -ge 2 ]] || die "usage: $0 <ubuntu-base-arm64.tar.gz> <workdir> [--real-install-node] [--skip-readsb]"
BASE_TAR="$1" WORK="$2"; shift 2
REAL=0 SKIP_READSB=()
for a in "$@"; do
    case "$a" in
        --real-install-node) REAL=1 ;;
        --skip-readsb) SKIP_READSB=(--skip-readsb) ;;
        *) die "unknown argument $a" ;;
    esac
done
require_root
require_binfmt
install -d "$WORK"
WORK="$(cd "$WORK" && pwd)"
ROOTFS="$WORK/rootfs"
VERSION=0.0.0-localtest
URL=https://enroll.example.org

step "fresh rootfs from $(basename "$BASE_TAR")"
if [[ -d "$ROOTFS" ]]; then
    for m in run dev/pts dev sys proc; do mountpoint -q "$ROOTFS/$m" && umount -l "$ROOTFS/$m"; done
    rm -rf "$ROOTFS"
fi
install -d "$ROOTFS"
tar -xpzf "$BASE_TAR" -C "$ROOTFS" --numeric-owner

step "test-only apt setup (HTTPS mirror, proxy, CA)"
sed -i 's#http://ports.ubuntu.com#https://ports.ubuntu.com#g' "$ROOTFS/etc/apt/sources.list"
CA="${SSL_CERT_FILE:-/root/.ccr/ca-bundle.crt}"
if [[ -n "${HTTPS_PROXY:-${https_proxy:-}}" && -f "$CA" ]]; then
    install -D -m 0644 "$CA" "$ROOTFS/etc/ssl/uapf-test-ca.crt"
    cat >"$ROOTFS/etc/apt/apt.conf.d/99uapf-test-proxy" <<EOF
Acquire::https::Proxy "${HTTPS_PROXY:-${https_proxy:-}}";
Acquire::https::CAInfo "/etc/ssl/uapf-test-ca.crt";
EOF
fi
# pip (in the real install-node.sh) uses its own CA bundle: point it at the proxy CA.
[[ ! -f "$ROOTFS/etc/ssl/uapf-test-ca.crt" ]] || export PIP_CERT=/etc/ssl/uapf-test-ca.crt

step "what NVIDIA's minimal rootfs + apply_binaries + l4t_create_default_user provide (systemd, a kernel, the user)"
trap chroot_leave EXIT
chroot_enter "$ROOTFS"
in_chroot apt-get update
in_chroot apt-get install -y --no-install-recommends systemd systemd-sysv udev dbus sudo passwd kmod \
    ca-certificates linux-headers-generic
KVER="$(basename "$(find "$ROOTFS/usr/src" -maxdepth 1 -name 'linux-headers-*-generic' | sort -V | tail -n1)")"
KVER="${KVER#linux-headers-}"
[[ -n "$KVER" ]] || die "no generic kernel headers installed"
log "kernel headers for the module build test: $KVER"
in_chroot useradd -m -s /bin/bash -G sudo "$DEFAULT_USER"
echo "$DEFAULT_USER:throwaway" | in_chroot chpasswd
echo "$DEFAULT_HOSTNAME" >"$ROOTFS/etc/hostname"
# A stand-in extlinux.conf, as L4T has.
install -d "$ROOTFS/boot/extlinux"
# shellcheck disable=SC2016  # ${cbootargs} is literal extlinux syntax
printf 'TIMEOUT 30\nDEFAULT primary\n\nMENU TITLE L4T boot options\n\nLABEL primary\n      MENU LABEL primary kernel\n      LINUX /boot/Image\n      INITRD /boot/initrd\n      APPEND ${cbootargs} root=/dev/mmcblk0p1 rw rootwait\n' \
    >"$ROOTFS/boot/extlinux/extlinux.conf"
chroot_leave
trap - EXIT

step "customize_rootfs.sh --test"
INSTALL_NODE=edge/deploy/image/tests/stub-install-node.sh
[[ $REAL == 0 ]] || INSTALL_NODE=edge/deploy/install-node.sh
UAPF_INSTALL_NODE="$INSTALL_NODE" UAPF_PLATFORM_TEST=1 UAPF_FORCE_PPS_BUILD=1 UAPF_KVERS="$KVER" \
    "$IMAGE_DIR/customize_rootfs.sh" --rootfs "$ROOTFS" --repo "$REPO" --version "$VERSION" \
    --enroll-url "$URL" --skip-tailscale "${SKIP_READSB[@]}" --test
rm -f "$ROOTFS/etc/apt/apt.conf.d/99uapf-test-proxy" "$ROOTFS/etc/ssl/uapf-test-ca.crt"

step "pps-gpio module built for $KVER"
ko="$ROOTFS/lib/modules/$KVER/updates/uapfinder/pps-gpio.ko"
[[ -f "$ko" ]] || die "no $ko"
file "$ko"
modinfo "$ko" | grep -E '^(vermagic|alias|license):'

step "test-image.sh on the root file system"
"$IMAGE_DIR/test-image.sh" --rootfs "$ROOTFS" --local-test --expect-version "$VERSION" --expect-enroll-url "$URL"

step "pack a GPT image with an APP partition and test it"
IMG="$WORK/uapfinder-node-$VERSION-orin-nano.img"
mb=$(( $(du -sm "$ROOTFS" | cut -f1) * 12 / 10 + 256 ))
rm -f "$IMG" "$IMG.xz"
truncate -s $(( (mb + 2) * 1024 * 1024 )) "$IMG"
sgdisk -og "$IMG" >/dev/null
sgdisk -n 1:2048:+"${mb}M" -c 1:APP -t 1:8300 "$IMG" >/dev/null
truncate -s "${mb}M" "$WORK/app.ext4"
mkfs.ext4 -q -F -L APP -d "$ROOTFS" "$WORK/app.ext4"
dd if="$WORK/app.ext4" of="$IMG" bs=1M seek=1 conv=notrunc,sparse status=none
rm -f "$WORK/app.ext4"
"$IMAGE_DIR/test-image.sh" "$IMG" --local-test --expect-version "$VERSION" --expect-enroll-url "$URL"
xz -T0 -1 -k "$IMG"
"$IMAGE_DIR/test-image.sh" "$IMG.xz" --local-test --expect-version "$VERSION" --expect-enroll-url "$URL"
ls -lh "$IMG" "$IMG.xz"
log "local chroot test passed"
