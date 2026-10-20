#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Build and install readsb (ADS-B decoder, https://github.com/wiedehopf/readsb)
# as a Debian package with RTL-SDR support, the way its README describes,
# and leave it disabled: a udev rule starts it when an RTL-SDR receiver
# (0bda:2838 or 0bda:2832) is plugged in.
#
#   sudo install-readsb.sh [--src DIR]    (on the node, or inside the image chroot)
#
# --src DIR: an already checked-out source tree (the image build clones and
# verifies it on the host); otherwise READSB_TAG/READSB_COMMIT from
# versions.env are cloned with git. Build dependencies that were not
# installed before are marked automatically installed, so the image build's
# final autoremove takes them out again. Idempotent: skips the build when
# the same version is already installed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
# shellcheck source=versions.env
. "$SCRIPT_DIR/versions.env"
require_root
export DEBIAN_FRONTEND=noninteractive

SRC=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --src) SRC="${2:?--src needs a directory}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

want_version="${READSB_TAG#v}"
have_version="$(dpkg-query -W -f '${Version}' readsb 2>/dev/null || true)"
if [[ -n "$have_version" && "$have_version" == "$want_version"* ]]; then
    log "readsb $have_version already installed"
else
    BUILD_DEPS=(git build-essential debhelper pkg-config fakeroot help2man
        libncurses-dev zlib1g-dev libzstd-dev libusb-1.0-0-dev librtlsdr-dev)
    NEW_DEPS=()
    for p in "${BUILD_DEPS[@]}"; do
        dpkg-query -W -f '${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || NEW_DEPS+=("$p")
    done
    if [[ ${#NEW_DEPS[@]} -gt 0 ]]; then
        log "installing readsb build dependencies: ${NEW_DEPS[*]}"
        apt-get install -y --no-install-recommends --no-install-suggests "${NEW_DEPS[@]}"
    fi
    WORK="$(mktemp -d /tmp/readsb-build.XXXXXX)"
    if [[ -z "$SRC" ]]; then
        git clone --quiet --depth 1 --branch "$READSB_TAG" "$READSB_REPO" "$WORK/readsb"
        got="$(git -C "$WORK/readsb" rev-parse HEAD)"
        [[ "$got" == "$READSB_COMMIT" ]] || die "readsb $READSB_TAG is commit $got, expected $READSB_COMMIT"
        SRC="$WORK/readsb"
    else
        cp -a "$SRC" "$WORK/readsb"
        SRC="$WORK/readsb"
    fi
    log "building readsb $READSB_TAG (dpkg-buildpackage, rtlsdr profile)"
    (cd "$SRC" && DEB_BUILD_OPTIONS="noddebs parallel=$(nproc)" \
        dpkg-buildpackage -b -ui -uc -us --build-profiles=rtlsdr >"$WORK/build.log" 2>&1) \
        || { tail -n 60 "$WORK/build.log" >&2; die "readsb build failed"; }
    deb="$(find "$WORK" -maxdepth 1 -name 'readsb_*.deb' | head -n1)"
    [[ -n "$deb" ]] || die "readsb build produced no .deb"
    apt-get install -y --no-install-recommends "$deb"
    rm -rf "$WORK"
    if [[ ${#NEW_DEPS[@]} -gt 0 ]]; then apt-mark auto "${NEW_DEPS[@]}" >/dev/null; fi
fi

# Off until a receiver is present.
systemctl disable readsb.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/default.target.wants/readsb.service \
    /etc/systemd/system/multi-user.target.wants/readsb.service

# The DVB-T kernel driver would claim the stick before librtlsdr can; a udev
# rule starts readsb when a receiver appears. edge/deploy/install-node.sh
# installs the same two files from edge/deploy/appliance/ (they win when both run).
APPLIANCE="$SCRIPT_DIR/../appliance"
if [[ -f "$APPLIANCE/90-uapfinder-rtlsdr.rules" && -f "$APPLIANCE/rtlsdr-blacklist.conf" ]]; then
    install -D -m 0644 "$APPLIANCE/rtlsdr-blacklist.conf" /etc/modprobe.d/uapfinder-rtlsdr-blacklist.conf
    install -D -m 0644 "$APPLIANCE/90-uapfinder-rtlsdr.rules" /etc/udev/rules.d/90-uapfinder-rtlsdr.rules
else
cat >/etc/modprobe.d/uapfinder-rtlsdr-blacklist.conf <<'EOF'
# Written by edge/deploy/image/install-readsb.sh: let librtlsdr (readsb) use
# RTL2832U sticks instead of the DVB-T TV driver.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
cat >/etc/udev/rules.d/90-uapfinder-rtlsdr.rules <<'EOF'
# Written by edge/deploy/image/install-readsb.sh: start readsb when an
# RTL-SDR ADS-B receiver is plugged in (also at boot if it is already there).
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2838|2832", GROUP="plugdev", MODE="0660", TAG+="systemd", ENV{SYSTEMD_WANTS}+="readsb.service"
EOF
fi
log "readsb $(dpkg-query -W -f '${Version}' readsb) installed, disabled; started by udev when a receiver is present"
