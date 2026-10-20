#!/usr/bin/env bash
# Build-time check that the UAP Finder overlays apply to the BSP's kernel
# DTBs for the Orin Nano developer kit, so a broken overlay fails the image
# build instead of a volunteer's boot.
#
#   check-overlays.sh <Linux_for_Tegra> [extra.dtbo ...]
#
# Compiles overlays/uapfinder-pps-pin7.dts, then for every kernel DTB of the
# devkit module (p3768-0000+p3767-0005, with and without "-super") found in
# Linux_for_Tegra/kernel/dtb: requires __symbols__, requires that the overlay
# lists the DTB's board compatible, and merges it (plus the IMX477 camera
# overlay shipped in the same directory, and any extra .dtbo given) with
# fdtoverlay. The merged tree must contain the pps-gpio node with the PAC.06
# GPIO specifier (166).
set -euo pipefail

log() { printf '[check-overlays] %s\n' "$*"; }
die() { printf '[check-overlays] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "usage: $0 <Linux_for_Tegra> [extra.dtbo ...]"
L4T="$1"; shift
DTB_DIR="$L4T/kernel/dtb"
[[ -d "$DTB_DIR" ]] || die "$DTB_DIR not found"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for t in dtc fdtget fdtoverlay; do command -v "$t" >/dev/null || die "$t not found (apt install device-tree-compiler)"; done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
dtc -@ -q -I dts -O dtb -o "$TMP/pps.dtbo" "$SCRIPT_DIR/overlays/uapfinder-pps-pin7.dts"

camera=""
for c in tegra234-p3767-camera-p3768-imx477-A.dtbo tegra234-p3767-camera-p3768-imx477-dual.dtbo; do
    if [[ -f "$DTB_DIR/$c" ]]; then camera="$DTB_DIR/$c"; break; fi
done
if [[ -n "$camera" ]]; then log "camera overlay: $(basename "$camera")"; else log "WARNING: no IMX477 overlay in $DTB_DIR"; fi

checked=0
shopt -s nullglob
for dtb in "$DTB_DIR"/tegra234-p3768-0000+p3767-0005*.dtb; do
    name="$(basename "$dtb")"
    compat="$(fdtget -t s "$dtb" / compatible | awk '{print $1}')"
    fdtget "$dtb" /__symbols__ gpio >/dev/null 2>&1 || die "$name has no __symbols__ (overlays cannot apply)"
    ovc="$(fdtget -t s "$TMP/pps.dtbo" / compatible)"
    grep -qw -- "$compat" <<<"${ovc// /$'\n'}" || die "the PPS overlay does not list $compat ($name)"
    overlays=("$TMP/pps.dtbo")
    [[ -z "$camera" ]] || overlays+=("$camera")
    overlays+=("$@")
    fdtoverlay -i "$dtb" -o "$TMP/merged.dtb" "${overlays[@]}" \
        || die "overlays do not apply to $name"
    got="$(fdtget -t u "$TMP/merged.dtb" /pps-uapfinder gpios 2>/dev/null | awk '{print $2}')"
    [[ "$got" == 166 ]] || die "merged $name: /pps-uapfinder gpios line is '$got', expected 166"
    log "ok: $name ($compat)"
    checked=$((checked + 1))
done
[[ $checked -gt 0 ]] || die "no tegra234-p3768-0000+p3767-0005*.dtb in $DTB_DIR"
log "$checked DTB(s) checked"
