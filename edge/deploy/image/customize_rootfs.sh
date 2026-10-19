#!/usr/bin/env bash
# Customise an L4T sample root filesystem for SKY-NET nodes before flashing.
#
#   sudo ./customize_rootfs.sh <Linux_for_Tegra/rootfs> <path to the uapfinder repository> [firstboot.env]
#
# Runs on an x86_64 Ubuntu host *after* apply_binaries.sh (which installs the
# NVIDIA L4T packages and the NVIDIA apt source into the rootfs). It chroots
# into the aarch64 rootfs through qemu-user-static, installs the packages the
# node needs, copies the software, and installs a one-shot first-boot unit
# that runs deploy/jetson/setup.sh on the device (udev, gpsd, chrony, PPS
# detection, Tailscale and the systemd service need the running hardware).
#
# firstboot.env (optional) is copied to /etc/skynet/firstboot.env (mode 0600)
# and sourced by the first-boot unit: SKYNET_NODE_ID, SKYNET_NODE_KEY,
# SKYNET_SERVER_HOST, TS_AUTHKEY, SKYNET_NVPMODEL ... (see setup.sh). It holds
# secrets; it is deleted after a successful first boot.
#
# NOT executed by the authors: read it, and test the image on one unit first.
set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root"
[[ $# -ge 2 ]] || die "usage: $0 <rootfs> <repo> [firstboot.env]"
ROOTFS="$(cd "$1" && pwd)"
REPO="$(cd "$2" && pwd)"
ENV_FILE="${3:-}"
[[ -x "$ROOTFS/bin/bash" && -d "$ROOTFS/etc/apt" ]] || die "$ROOTFS does not look like a root filesystem"
[[ -f "$REPO/edge/deploy/jetson/setup.sh" ]] || die "$REPO is not the uapfinder repository"
[[ -f "$ROOTFS/etc/apt/sources.list.d/nvidia-l4t-apt-source.list" ]] \
    || die "run Linux_for_Tegra/apply_binaries.sh first (NVIDIA apt source missing)"

# aarch64 binaries run through binfmt_misc + qemu-user-static (fix-binary mode on
# Ubuntu 22.04 hosts, so no emulator copy inside the rootfs is needed).
if [[ "$(uname -m)" != aarch64 && ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
    die "install qemu-user-static and binfmt-support on the host (apt install qemu-user-static binfmt-support)"
fi

cleanup() {
    for m in dev/pts dev sys proc; do
        mountpoint -q "$ROOTFS/$m" && umount -l "$ROOTFS/$m"
    done
    if [[ -f "$ROOTFS/etc/resolv.conf.skynet-orig" ]]; then
        mv -f "$ROOTFS/etc/resolv.conf.skynet-orig" "$ROOTFS/etc/resolv.conf"
    fi
}
trap cleanup EXIT
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sys "$ROOTFS/sys"
mount --bind /dev "$ROOTFS/dev"
mount --bind /dev/pts "$ROOTFS/dev/pts"
if [[ -e "$ROOTFS/etc/resolv.conf" || -L "$ROOTFS/etc/resolv.conf" ]]; then
    mv "$ROOTFS/etc/resolv.conf" "$ROOTFS/etc/resolv.conf.skynet-orig"
fi
cp -L /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

echo "[image] installing packages in the chroot"
chroot "$ROOTFS" /usr/bin/env DEBIAN_FRONTEND=noninteractive bash -euo pipefail -c '
    apt-get update
    apt-get install -y --no-install-recommends \
        python3-venv python3-pip python3-numpy python3-yaml python3-requests \
        gpsd gpsd-clients chrony pps-tools ffmpeg gstreamer1.0-tools i2c-tools rsync curl ca-certificates
    # JetPack OpenCV (GStreamer-enabled); Ubuntu python3-opencv as a fallback.
    apt-get install -y nvidia-opencv || apt-get install -y python3-opencv
    apt-get clean
'

echo "[image] copying the software"
install -d "$ROOTFS/opt/skynet"
rsync -a --delete --exclude .git --exclude .venv --exclude node_modules --exclude __pycache__ \
    --exclude '*.egg-info' "$REPO/common" "$REPO/edge" "$ROOTFS/opt/skynet/bootstrap/"
install -d -m 0755 "$ROOTFS/etc/skynet"
if [[ -n "$ENV_FILE" ]]; then
    install -m 0600 "$ENV_FILE" "$ROOTFS/etc/skynet/firstboot.env"
fi

echo "[image] installing the first-boot unit"
cat > "$ROOTFS/etc/systemd/system/skynet-firstboot.service" <<'EOF'
[Unit]
Description=SKY-NET first-boot provisioning (runs deploy/jetson/setup.sh once)
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/skynet/.provisioned

[Service]
Type=oneshot
EnvironmentFile=-/etc/skynet/firstboot.env
Environment=SKYNET_SKIP_APT=1
ExecStart=/opt/skynet/bootstrap/edge/deploy/jetson/setup.sh
ExecStartPost=/bin/sh -c 'touch /var/lib/skynet/.provisioned && rm -f /etc/skynet/firstboot.env'
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/skynet-firstboot.service \
    "$ROOTFS/etc/systemd/system/multi-user.target.wants/skynet-firstboot.service"
echo "[image] done: create the default user (tools/l4t_create_default_user.sh) and flash (see README.md)"
