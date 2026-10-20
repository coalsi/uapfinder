#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Check a built UAP Finder node image (or its root file system) without
# booting it: loop-mounts the APP partition read-only and checks the files,
# users, enabled units and defaults the image must have.
#
#   sudo test-image.sh IMAGE.img[.xz] [--expect-version V] [--expect-enroll-url URL] [--local-test]
#   sudo test-image.sh --rootfs DIR   [same options]
#
#   --local-test   the image was built from a plain Ubuntu base by the local
#                  chroot test (tests/local-chroot-test.sh): NVIDIA-specific
#                  checks become warnings.
# Exit status 0 when every check passes (warnings allowed), 1 otherwise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
. "$SCRIPT_DIR/versions.env"

IMAGE="" ROOT="" EXPECT_VERSION="" EXPECT_URL="" LOCAL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rootfs) ROOT="${2:?}"; shift 2 ;;
        --expect-version) EXPECT_VERSION="${2:?}"; shift 2 ;;
        --expect-enroll-url) EXPECT_URL="${2:?}"; shift 2 ;;
        --local-test) LOCAL=1; shift ;;
        -h | --help) sed -n '3,14p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done
[[ -n "$IMAGE" || -n "$ROOT" ]] || { echo "usage: $0 IMAGE.img[.xz] | --rootfs DIR" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run as root (loop mount)" >&2; exit 2; }

TMP="$(mktemp -d)"
MNT=""
cleanup() {
    if [[ -n "$MNT" ]] && mountpoint -q "$MNT"; then umount "$MNT" || umount -l "$MNT"; fi
    rm -rf "$TMP"
}
trap cleanup EXIT

if [[ -n "$IMAGE" ]]; then
    [[ -f "$IMAGE" ]] || { echo "$IMAGE not found" >&2; exit 2; }
    if [[ "$IMAGE" == *.xz ]]; then
        echo "decompressing $IMAGE to $TMP"
        xz -dc "$IMAGE" >"$TMP/image.img"
        IMAGE="$TMP/image.img"
    fi
    # The root file system is the GPT partition named APP.
    read -r START SECTORS < <(sfdisk --json "$IMAGE" | python3 -c '
import json, sys
t = json.load(sys.stdin)["partitiontable"]
for p in t.get("partitions", []):
    if p.get("name") == "APP":
        print(p["start"], p["size"]); break
')
    [[ -n "${START:-}" ]] || { echo "no partition named APP in $IMAGE" >&2; sfdisk -l "$IMAGE" >&2; exit 1; }
    MNT="$TMP/app"
    mkdir -p "$MNT"
    mount -o ro,loop,noload,offset=$((START * 512)),sizelimit=$((SECTORS * 512)) "$IMAGE" "$MNT" 2>/dev/null \
        || mount -o ro,loop,offset=$((START * 512)),sizelimit=$((SECTORS * 512)) "$IMAGE" "$MNT"
    ROOT="$MNT"
    echo "image: APP partition at sector $START, $((SECTORS / 2048)) MiB"
    echo "partitions: $(sfdisk --json "$IMAGE" | python3 -c 'import json,sys; print(" ".join(p.get("name","?") for p in json.load(sys.stdin)["partitiontable"]["partitions"]))')"
fi
ROOT="${ROOT%/}"
[[ -d "$ROOT/etc" ]] || { echo "$ROOT has no /etc" >&2; exit 1; }

PASS=0 FAIL=0 WARN=0
ok() { printf '  PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
meh() { printf '  WARN  %s\n' "$*"; WARN=$((WARN + 1)); }
# nvidia CHECK MESSAGE: a failure on a real image, a warning in the local test.
nv() { if [[ $LOCAL == 1 ]]; then meh "$* (local test)"; else bad "$*"; fi; }
check() { local msg="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$msg"; else bad "$msg"; fi; }

enabled() { # unit: a symlink to it in any *.wants directory under /etc/systemd/system
    find "$ROOT/etc/systemd/system" -path '*.wants/*' -name "$1" 2>/dev/null | grep -q .
}
dpkg_has() { dpkg-query --admindir="$ROOT/var/lib/dpkg" -W -f '${db:Status-Abbrev}' "$1" 2>/dev/null | grep -q '^ii'; }

echo "== base system"
if [[ -f "$ROOT/etc/nv_tegra_release" ]]; then
    rel="$(head -n1 "$ROOT/etc/nv_tegra_release")"
    if grep -q "R${L4T_VERSION%%.*} (release), REVISION: ${L4T_VERSION#*.}" <<<"$rel"; then ok "Jetson Linux $rel"; else bad "nv_tegra_release is '$rel', expected R$L4T_VERSION"; fi
else
    nv "no /etc/nv_tegra_release (not an L4T root file system)"
fi
if grep -q 'VERSION_ID="22.04"' "$ROOT/etc/os-release" 2>/dev/null; then ok "Ubuntu 22.04"; else bad "not Ubuntu 22.04"; fi
target="$(readlink "$ROOT/etc/systemd/system/default.target" 2>/dev/null || true)"
if [[ "$target" == */multi-user.target ]]; then ok "default target multi-user (no desktop)"; else bad "default target is '${target:-unset}'"; fi
desk=""
for p in ubuntu-desktop ubuntu-desktop-minimal gdm3 gnome-shell lightdm xserver-xorg-core snapd oem-config; do
    if dpkg_has "$p"; then desk+=" $p"; fi
done
if [[ -z "$desk" ]]; then ok "no desktop packages"; else bad "desktop packages installed:$desk"; fi
if compgen -G "$ROOT/etc/ssh/ssh_host_*" >/dev/null; then bad "SSH host keys present (every card would share them)"; else ok "no SSH host keys"; fi
if [[ ! -s "$ROOT/etc/machine-id" ]]; then ok "empty machine-id"; else bad "machine-id is set ($(cat "$ROOT/etc/machine-id"))"; fi
if [[ -z "$(find "$ROOT/var/lib/apt/lists" -type f ! -name lock ! -name '*.lock' 2>/dev/null | head -n1)" ]]; then ok "apt lists cleaned"; else meh "apt lists not cleaned"; fi
if [[ -e "$ROOT/usr/sbin/policy-rc.d" ]]; then bad "build-time policy-rc.d left behind"; else ok "no policy-rc.d"; fi
oem="$(find "$ROOT/etc/systemd/system" -path '*.wants/*' -name '*oem-config*' 2>/dev/null | head -n1)"
if [[ -z "$oem" ]]; then ok "NVIDIA first-boot wizard (oem-config) not enabled"; else bad "oem-config enabled: ${oem#"$ROOT"}"; fi

echo "== accounts"
if grep -q "^${DEFAULT_USER}:" "$ROOT/etc/passwd"; then
    ok "default account $DEFAULT_USER exists"
    pwfield="$(awk -F: -v u="$DEFAULT_USER" '$1 == u {print $2}' "$ROOT/etc/shadow")"
    if [[ "$pwfield" == '!'* || "$pwfield" == '*'* ]]; then ok "$DEFAULT_USER password locked"; else bad "$DEFAULT_USER has a usable password"; fi
    if awk -F: '$1 == "sudo" {print $4}' "$ROOT/etc/group" | tr ',' '\n' | grep -qx "$DEFAULT_USER"; then ok "$DEFAULT_USER in sudo"; else meh "$DEFAULT_USER not in sudo"; fi
else
    bad "default account $DEFAULT_USER missing"
fi
check "service user skynet exists" grep -q '^skynet:' "$ROOT/etc/passwd"

echo "== node software and configuration"
check "/opt/skynet/venv/bin/python" test -e "$ROOT/opt/skynet/venv/bin/python"
if [[ -f "$ROOT/etc/skynet/defaults.yaml" ]]; then
    url="$(sed -n -E 's/^enroll_url:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/p' "$ROOT/etc/skynet/defaults.yaml" | head -n1)"
    if [[ -n "$EXPECT_URL" ]]; then
        if [[ "$url" == "$EXPECT_URL" ]]; then ok "defaults.yaml enroll_url = $url"; else bad "defaults.yaml enroll_url is '$url', expected '$EXPECT_URL'"; fi
    elif [[ -n "$url" ]]; then ok "defaults.yaml enroll_url = $url"; else bad "defaults.yaml has no enroll_url"; fi
else
    bad "/etc/skynet/defaults.yaml missing"
fi
if [[ -f "$ROOT/etc/uapfinder-release" ]]; then
    v="$(sed -n 's/^UAPF_IMAGE_VERSION=//p' "$ROOT/etc/uapfinder-release")"
    if [[ -z "$EXPECT_VERSION" || "$v" == "$EXPECT_VERSION" ]]; then ok "uapfinder-release version $v"; else bad "uapfinder-release version '$v', expected '$EXPECT_VERSION'"; fi
else
    bad "/etc/uapfinder-release missing"
fi

echo "== units"
for u in uapfinder-firstboot.service uapfinder-netsetup.service uapfinder-setup.service \
    skynet-edge.service uapfinder-starcal.timer uapfinder-overlays.service uapfinder-admin.path \
    NetworkManager.service avahi-daemon.service chrony.service; do
    if enabled "$u"; then ok "enabled: $u"; else bad "not enabled: $u"; fi
done
if enabled gpsd.service || enabled gpsd.socket; then ok "enabled: gpsd"; else meh "gpsd not enabled (install-node.sh decides)"; fi
for u in readsb.service tailscaled.service; do
    if enabled "$u"; then bad "$u enabled (must stay off until needed)"; else ok "disabled: $u"; fi
done
if enabled ssh.service || enabled ssh.socket; then meh "ssh enabled in the image (expected off until the device password is set)"; else ok "ssh off"; fi

echo "== bundled programs"
if [[ -x "$ROOT/usr/sbin/tailscaled" || -x "$ROOT/usr/bin/tailscale" ]]; then ok "tailscale installed"; else nv "tailscale missing"; fi
check "readsb installed" test -x "$ROOT/usr/bin/readsb"
check "readsb udev rule" test -f "$ROOT/etc/udev/rules.d/90-uapfinder-rtlsdr.rules"
for b in chronyd gpsd ffmpeg gst-launch-1.0 nmcli avahi-daemon fdtoverlay ppstest growpart; do
    if [[ -x "$ROOT/usr/sbin/$b" || -x "$ROOT/usr/bin/$b" || -x "$ROOT/sbin/$b" ]]; then ok "$b"; else bad "$b missing"; fi
done

echo "== platform (PPS and camera)"
check "PPS overlay /boot/uapfinder-pps-pin7.dtbo" test -s "$ROOT/boot/uapfinder-pps-pin7.dtbo"
if command -v fdtget >/dev/null && [[ -s "$ROOT/boot/uapfinder-pps-pin7.dtbo" ]]; then
    if fdtget -t s "$ROOT/boot/uapfinder-pps-pin7.dtbo" / compatible | grep -q 'p3768-0000+p3767-0005'; then ok "PPS overlay lists the devkit"; else bad "PPS overlay compatible wrong"; fi
fi
check "configure-overlays.sh installed" test -x "$ROOT/usr/lib/uapfinder/platform/configure-overlays.sh"
if [[ -f "$ROOT/etc/uapfinder/boot-overlays" ]]; then
    ok "overlay list: $(grep -v '^#' "$ROOT/etc/uapfinder/boot-overlays" | tr '\n' ' ')"
    if grep -q 'imx477' "$ROOT/etc/uapfinder/boot-overlays"; then ok "IMX477 camera overlay listed"; else nv "no IMX477 camera overlay listed"; fi
    while read -r ov; do
        [[ -z "$ov" || "$ov" == '#'* ]] && continue
        if [[ -f "$ROOT$ov" ]]; then ok "overlay present: $ov"; else bad "listed overlay missing: $ov"; fi
    done <"$ROOT/etc/uapfinder/boot-overlays"
else
    bad "/etc/uapfinder/boot-overlays missing"
fi
mode="$(sed -n 's/^PPS_MODE=//p' "$ROOT/usr/lib/uapfinder/platform/installed.env" 2>/dev/null || true)"
case "$mode" in
    builtin | module | built) ok "pps-gpio: $mode" ;;
    *) nv "pps-gpio not available (PPS_MODE=${mode:-unknown})" ;;
esac
if [[ -f "$ROOT/boot/extlinux/extlinux.conf" ]]; then
    if grep -q '^DEFAULT primary' "$ROOT/boot/extlinux/extlinux.conf"; then ok "extlinux DEFAULT primary (overlay entry added at first boot)"; else meh "extlinux DEFAULT is $(grep '^DEFAULT' "$ROOT/boot/extlinux/extlinux.conf")"; fi
else
    nv "no /boot/extlinux/extlinux.conf"
fi
u=nvresizefs.service
if [[ -e "$ROOT/etc/systemd/system/$u" || -e "$ROOT/lib/systemd/system/$u" ]]; then
    echo "  INFO  NVIDIA $u present (enabled: $(enabled "$u" && echo yes || echo no)); uapfinder-firstboot is ordered after it"
fi

echo
echo "== $PASS passed, $WARN warnings, $FAIL failed"
[[ $FAIL -eq 0 ]]
