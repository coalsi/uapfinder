#!/usr/bin/env bash
# UAP Finder / SKY-NET node installer: the same system for the flashable image
# (run in the image chroot with --chroot) and for a Jetson that already runs
# JetPack 6 (run directly, or through edge/deploy/jetson/setup.sh).
#
# Idempotent and non-interactive: re-run it after a `git pull` to update.
# Kept on re-runs: /etc/skynet/edge.local.yaml, /etc/skynet/defaults.yaml (except
# enroll_url when UAPF_ENROLL_URL is given), /var/lib/skynet (credentials,
# calibration, clips, the setup page's password).
#
# NOT executed on Jetson hardware by the authors. Tested in an Ubuntu 22.04
# container (see edge/README.md): packages, user, venv, units, configs.
#
# Usage:  sudo edge/deploy/install-node.sh [--chroot]
#   --chroot   building an image: no running systemd, udev or network services are
#              touched (units are only enabled), no hardware is probed.
#
# Environment (all optional):
#   UAPF_ENROLL_URL      enrollment server written to /etc/skynet/defaults.yaml
#   SKYNET_GPS_TTY       header GPS serial device (default ttyTHS1 = pins 8/10 on JetPack 6)
#   SKYNET_GPS_BAUD      GPS baud rate (default 9600)
#   SKYNET_PPS_DEVICE    force a PPS device (default: detected at every boot)
#   SKYNET_NVPMODEL      nvpmodel mode id to set (live system only)
#   SKYNET_CUDA=1        also install CuPy for the CUDA detection backend
#   SKYNET_SKIP_APT=1    skip apt-get (faster re-runs)
#   SKYNET_SKIP_TAILSCALE=1  do not install Tailscale
set -euo pipefail

log() { printf '\033[1;34m[uapfinder]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[uapfinder] WARNING:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[uapfinder] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CHROOT=0
for arg in "$@"; do
    case "$arg" in
        --chroot) CHROOT=1 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown argument: $arg" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
[[ -f "$REPO/edge/pyproject.toml" && -f "$REPO/common/pyproject.toml" ]] || die "cannot find the repository at $REPO"
DEPLOY="$REPO/edge/deploy"

GPS_TTY="${SKYNET_GPS_TTY:-ttyTHS1}"
GPS_BAUD="${SKYNET_GPS_BAUD:-9600}"
[[ "$GPS_TTY" =~ ^tty[A-Za-z0-9]{1,16}$ ]] || die "SKYNET_GPS_TTY must look like ttyTHS1"
[[ "$GPS_BAUD" =~ ^(4800|9600|19200|38400|57600|115200)$ ]] || die "SKYNET_GPS_BAUD: unsupported rate"
PREFIX=/opt/skynet
VENV="$PREFIX/venv"
ETC=/etc/skynet
STATE=/var/lib/skynet
UNITS_ENABLE=(skynet-edge.service uapfinder-firstboot.service uapfinder-netsetup.service uapfinder-setup.service
              uapfinder-starcal.timer uapfinder-admin.path)

if [[ -f /etc/nv_tegra_release ]]; then
    log "Jetson: $(head -n1 /etc/nv_tegra_release)"
    grep -q "R36" /etc/nv_tegra_release || warn "expected L4T R36 (JetPack 6.x); continuing anyway"
else
    warn "/etc/nv_tegra_release not found: not a Jetson (fine for a container test)"
fi
[[ $CHROOT == 1 ]] && log "chroot mode: no services are started, no hardware is probed"

# --- 1. packages ----------------------------------------------------------------------
if [[ "${SKYNET_SKIP_APT:-0}" != 1 ]]; then
    log "installing packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # gpsd + chrony + pps-tools: GPS position and PPS-disciplined time; ffmpeg: clip
    # encoding; gstreamer1.0-tools: gst-launch fallback capture; i2c-tools, v4l-utils,
    # usbutils: diagnostics; network-manager + avahi: Wi-Fi/hotspot and uapfinder.local;
    # cloud-guest-utils (growpart), gdisk (sgdisk), parted (partprobe), e2fsprogs:
    # first boot; policykit-1: the setup page's NetworkManager grant.
    apt-get install -y --no-install-recommends \
        python3-venv python3-pip python3-numpy python3-yaml python3-requests \
        gpsd gpsd-clients chrony pps-tools ffmpeg gstreamer1.0-tools i2c-tools v4l-utils usbutils \
        network-manager avahi-daemon avahi-utils libnss-mdns policykit-1 openssh-server iw \
        cloud-guest-utils gdisk parted e2fsprogs util-linux rsync curl ca-certificates gnupg
    if ! python3 -c "import cv2" 2>/dev/null; then
        log "OpenCV for Python not found; installing JetPack's (or Ubuntu's) build"
        apt-get install -y nvidia-opencv || apt-get install -y --no-install-recommends python3-opencv \
            || die "no OpenCV available: install the JetPack OpenCV component (apt install nvidia-jetpack)"
    fi
    if [[ "${SKYNET_SKIP_TAILSCALE:-0}" != 1 ]] && ! command -v tailscale >/dev/null; then
        # Tailscale's documented manual install for Ubuntu 22.04 (jammy): signed apt repository.
        log "installing Tailscale from pkgs.tailscale.com"
        curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg \
            -o /usr/share/keyrings/tailscale-archive-keyring.gpg
        curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list \
            -o /etc/apt/sources.list.d/tailscale.list
        apt-get update
        apt-get install -y --no-install-recommends tailscale || warn "Tailscale install failed"
    fi
fi

# --- 2. user and directories ---------------------------------------------------------------
if ! id skynet >/dev/null 2>&1; then
    log "creating system user skynet"
    useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --user-group skynet
fi
# Camera (video), GPU (render), IMU (i2c), GPS serial (dialout), GPIO (gpio),
# journal for diagnostics (systemd-journal), USB devices (plugdev): only groups that exist.
for g in video render i2c dialout gpio systemd-journal plugdev netdev; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" skynet; fi
done
install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$ETC" /var/lib/uapfinder
install -d -m 0750 -o skynet -g skynet "$STATE" "$STATE/clips"
install -d -m 0700 -o skynet -g skynet "$STATE/setup"

# --- 3. software ------------------------------------------------------------------------------
log "installing skynet-edge into $VENV"
install -d -m 0755 "$PREFIX/src"
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude '*.egg-info' --exclude .pytest_cache \
    --exclude build "$REPO/common" "$REPO/edge" "$PREFIX/src/"
install -d -m 0755 "$PREFIX/src/docs"
cp "$REPO/docs/NODE_APPLIANCE.md" "$PREFIX/src/docs/" 2>/dev/null || true
# --system-site-packages: use JetPack's OpenCV (GStreamer-enabled, built against
# the system numpy 1.x) instead of a pip wheel that would shadow it.
[[ -x "$VENV/bin/python" ]] || python3 -m venv --system-site-packages "$VENV"
CONSTRAINTS="$PREFIX/constraints.txt"
echo "numpy<2" >"$CONSTRAINTS"  # the JetPack cv2 module fails to import under numpy 2.x
"$VENV/bin/pip" install --upgrade --quiet "pip>=23"
# --no-deps: keep pip from replacing the numpy the JetPack OpenCV was built against.
"$VENV/bin/pip" install --quiet --no-deps "$PREFIX/src/common"
"$VENV/bin/pip" install --quiet -c "$CONSTRAINTS" "$PREFIX/src/edge[serial,imu]"
if [[ "${SKYNET_CUDA:-0}" == 1 ]]; then
    log "installing CuPy (CUDA detection backend; NOT hardware-tested)"
    "$VENV/bin/pip" install --quiet -c "$CONSTRAINTS" "cupy-cuda12x>=13" \
        || warn "CuPy install failed; the node will use the CPU backend"
fi
"$VENV/bin/python" -c "import skynet_edge.appliance.web.server, skynet_edge.cli" \
    || die "the installed skynet-edge does not import"
install -m 0755 "$DEPLOY/appliance/uapfinder-firstboot.sh" "$PREFIX/bin/uapfinder-firstboot"

# --- 4. configuration ------------------------------------------------------------------------------
# Image defaults: the enrollment server and the online ADS-B feed (see appliance/autoconfig.py).
UAPF_ENROLL_URL="${UAPF_ENROLL_URL:-}" SKYNET_PPS_DEVICE="${SKYNET_PPS_DEVICE:-}" "$VENV/bin/python" - "$ETC/defaults.yaml" <<'EOF'
import os, sys, yaml
from skynet_edge.appliance.credentials import validate_url
from skynet_edge.appliance.util import atomic_write
path = sys.argv[1]
try:
    data = yaml.safe_load(open(path)) or {}
except FileNotFoundError:
    data = {"enroll_url": None, "adsb_online": True}
url = os.environ.get("UAPF_ENROLL_URL", "").strip()
if url:
    data["enroll_url"] = validate_url(url, "UAPF_ENROLL_URL")
pps = os.environ.get("SKYNET_PPS_DEVICE", "").strip()
if pps:
    data["pps_device"] = pps
head = ("# UAP Finder image defaults (edge/deploy/install-node.sh). enroll_url: the server that\n"
        "# claim codes are sent to. Keys: see skynet_edge/appliance/autoconfig.py DEFAULTS.\n")
atomic_write(path, head + yaml.safe_dump(data, sort_keys=True), 0o644)
print(f"[uapfinder] {path}: enroll_url = {data.get('enroll_url')}")
EOF
# A hand-written edge.yaml from the old setup.sh becomes the local override file,
# so autoconfig can own edge.yaml without losing anything.
if [[ -f "$ETC/edge.yaml" ]] && ! grep -q "Generated by \`skynet-edge autoconfig\`" "$ETC/edge.yaml"; then
    if [[ -f "$ETC/edge.local.yaml" ]]; then
        warn "$ETC/edge.yaml is hand-written and $ETC/edge.local.yaml exists: keeping edge.yaml as edge.yaml.bak"
        mv "$ETC/edge.yaml" "$ETC/edge.yaml.bak"
    else
        log "moving the hand-written $ETC/edge.yaml to $ETC/edge.local.yaml (it overrides the generated file)"
        mv "$ETC/edge.yaml" "$ETC/edge.local.yaml"
    fi
fi

# --- 5. GPS, time, udev, polkit, tmpfiles ----------------------------------------------------------------
USBAUTO=true
sed -e "s/@GPS_TTY@/${GPS_TTY}/g" "$DEPLOY/jetson/99-skynet-gps.rules" >/etc/udev/rules.d/99-skynet-gps.rules
# readsb (local ADS-B) is started by this udev rule only while an RTL-SDR is plugged in.
install -m 0644 "$DEPLOY/appliance/90-uapfinder-rtlsdr.rules" /etc/udev/rules.d/90-uapfinder-rtlsdr.rules
install -d /etc/modprobe.d
install -m 0644 "$DEPLOY/appliance/rtlsdr-blacklist.conf" /etc/modprobe.d/uapfinder-rtlsdr-blacklist.conf
sed -e "s/@GPS_BAUD@/${GPS_BAUD}/g" -e "s/@USBAUTO@/${USBAUTO}/g" "$DEPLOY/jetson/gpsd.default" >/etc/default/gpsd
install -d /etc/chrony/conf.d
if [[ ! -f /etc/chrony/conf.d/skynet-gps.conf ]]; then
    # Replaced at every node start by `skynet-edge autoconfig --system` once a PPS device is seen.
    cp "$DEPLOY/jetson/chrony-gps-nopps.conf" /etc/chrony/conf.d/skynet-gps.conf
fi
if [[ -f /etc/chrony/chrony.conf ]]; then
    grep -qE '^\s*confdir\s+/etc/chrony/conf.d' /etc/chrony/chrony.conf \
        || grep -qF 'include /etc/chrony/conf.d/*.conf' /etc/chrony/chrony.conf \
        || echo 'include /etc/chrony/conf.d/*.conf' >>/etc/chrony/chrony.conf
fi
install -d /etc/polkit-1/localauthority/50-local.d /etc/polkit-1/rules.d
install -m 0644 "$DEPLOY/appliance/50-uapfinder-networkmanager.pkla" \
    /etc/polkit-1/localauthority/50-local.d/50-uapfinder-networkmanager.pkla
install -m 0644 "$DEPLOY/appliance/50-uapfinder-networkmanager.rules" /etc/polkit-1/rules.d/50-uapfinder-networkmanager.rules
install -d /etc/tmpfiles.d
install -m 0644 "$DEPLOY/appliance/tmpfiles-uapfinder.conf" /etc/tmpfiles.d/uapfinder.conf
# Older installs gave the node a sudoers rule; nothing here needs sudo any more.
rm -f /etc/sudoers.d/uapfinder

# --- 6. systemd units ---------------------------------------------------------------------------------------
log "installing systemd units"
for u in "$DEPLOY"/systemd/*.service "$DEPLOY"/systemd/*.timer "$DEPLOY"/systemd/*.path; do
    install -m 0644 "$u" "/etc/systemd/system/$(basename "$u")"
done
# `systemctl enable` only creates symlinks, so it also works in a chroot without systemd running.
systemctl enable "${UNITS_ENABLE[@]}" >/dev/null
# tailscaled is left alone: `skynet-edge tailscale-join` enables it once enrollment returns a key.
for u in gpsd.socket gpsd.service chrony.service avahi-daemon.service NetworkManager.service; do
    if [[ -f "/lib/systemd/system/$u" || -f "/usr/lib/systemd/system/$u" || -f "/etc/systemd/system/$u" ]]; then
        systemctl enable "$u" >/dev/null 2>&1 || warn "could not enable $u"
    fi
done

# --- 7. platform (kernel module, device-tree overlays; owned by the OS image) --------------------------------
if [[ -f "$DEPLOY/platform/install-platform.sh" ]]; then
    log "platform: $DEPLOY/platform/install-platform.sh $*"
    bash "$DEPLOY/platform/install-platform.sh" "$@"
fi

# --- 8. live system only --------------------------------------------------------------------------------------
if [[ $CHROOT == 0 ]]; then
    if [[ -d /run/systemd/system ]]; then
        systemctl daemon-reload
        systemd-tmpfiles --create /etc/tmpfiles.d/uapfinder.conf || true
        udevadm control --reload-rules || true
        udevadm trigger --subsystem-match=tty --subsystem-match=pps --subsystem-match=usb || true
        if [[ -e "/dev/${GPS_TTY}" ]]; then stty -F "/dev/${GPS_TTY}" "$GPS_BAUD" || true; fi
        systemctl restart gpsd.service || warn "gpsd did not start (is the GPS connected?)"
        "$VENV/bin/skynet-edge" autoconfig --system || warn "autoconfig failed (see above)"
        systemctl restart chrony.service || warn "chrony did not restart"
        if [[ ! -e /var/lib/uapfinder/firstboot.done ]]; then
            # An existing system keeps its host name, SSH host keys and partition sizes;
            # only the blank-NVMe step of uapfinder-firstboot runs here and at each boot.
            log "existing system: keeping the host name and SSH host keys"
            date -u +%FT%TZ >/var/lib/uapfinder/firstboot.done
        fi
        systemctl start uapfinder-firstboot.service || warn "uapfinder-firstboot failed: journalctl -u uapfinder-firstboot"
        systemctl restart uapfinder-admin.path uapfinder-setup.service uapfinder-netsetup.service skynet-edge.service \
            || warn "a node service did not start: systemctl status 'uapfinder-*' skynet-edge"
        systemctl start uapfinder-starcal.timer || true
    else
        warn "systemd is not running here: units are enabled and start at the next boot"
    fi
    if [[ -n "${SKYNET_NVPMODEL:-}" ]]; then
        if command -v nvpmodel >/dev/null; then
            log "setting nvpmodel mode ${SKYNET_NVPMODEL} (persists across reboots)"
            nvpmodel -m "${SKYNET_NVPMODEL}" || warn "nvpmodel -m ${SKYNET_NVPMODEL} failed (see nvpmodel -q --verbose)"
        else
            warn "SKYNET_NVPMODEL set but nvpmodel is not available"
        fi
    fi
fi

log "installed. Open http://uapfinder.local (or http://$(hostname).local) to enroll the node."
