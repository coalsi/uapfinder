#!/usr/bin/env bash
# Configure the device-tree overlays the UAP Finder node needs (GPS PPS on
# header pin 7, IMX477 camera on CAM0) as a separate, reversible boot entry
# in /boot/extlinux/extlinux.conf. Installed by install-platform.sh to
# /usr/lib/uapfinder/platform/configure-overlays.sh and run at every boot by
# uapfinder-overlays.service (before the node's own first-boot service).
#
#   configure-overlays.sh [--reboot-if-changed] [--root DIR] [--compatible STR] [--dry-run]
#
# What it does, in order (each step is safe to repeat):
#  1. Reads the board's compatible string (/proc/device-tree/compatible, or
#     --compatible) and finds the kernel DTB for exactly that board in
#     /boot/dtb or /boot (matched by the DTB's own compatible, not by name).
#     L4T's UEFI only applies OVERLAYS on top of an FDT given in the same
#     entry, and that DTB must carry __symbols__.
#  2. Reads the overlay list (/etc/uapfinder/boot-overlays, one /boot path
#     per line) and keeps only overlays that exist, list this board in their
#     compatible property, and merge cleanly with fdtoverlay on top of that
#     DTB together with the overlays already kept.
#  3. Writes a "uapfinder" entry (a copy of the stock entry's LINUX, INITRD
#     and APPEND, plus FDT and OVERLAYS) between marker comments, and points
#     DEFAULT at it. The stock "primary" entry is never changed and stays in
#     the boot menu as the fallback (choose it on the serial console or a
#     monitor + keyboard during the boot-menu timeout).
#  4. With --reboot-if-changed, reboots once so the overlays take effect
#     (only if the file changed, and never twice for the same entry).
#
# Reversal: create /etc/uapfinder/overlays.disabled (or run with
# UAPF_OVERLAYS_DISABLE=1); the next run removes the entry and restores the
# previous DEFAULT. If DEFAULT names some other entry (for example one written
# by NVIDIA's jetson-io), the file is left alone.
#
# NOT hardware-tested: exercised against DTBs and an extlinux.conf on an x86
# host only.
set -euo pipefail

log() { printf '[uapfinder-overlays] %s\n' "$*"; }
warn() { printf '[uapfinder-overlays] WARNING: %s\n' "$*" >&2; }
die() { printf '[uapfinder-overlays] ERROR: %s\n' "$*" >&2; exit 1; }

ROOT=""
COMPAT=""
REBOOT=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) ROOT="${2:?--root needs a directory}"; shift 2 ;;
        --compatible) COMPAT="${2:?--compatible needs a string}"; shift 2 ;;
        --reboot-if-changed) REBOOT=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) sed -n '2,35p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
ROOT="${ROOT%/}"
[[ -z "$ROOT" ]] || [[ -d "$ROOT" ]] || die "--root $ROOT is not a directory"

EXTLINUX="$ROOT/boot/extlinux/extlinux.conf"
LIST="$ROOT/etc/uapfinder/boot-overlays"
DISABLED="$ROOT/etc/uapfinder/overlays.disabled"
STATE="$ROOT/var/lib/uapfinder/platform"
LABEL=uapfinder
BEGIN_MARK="# BEGIN uapfinder overlays (managed by configure-overlays.sh; see /etc/uapfinder/boot-overlays)"
END_MARK="# END uapfinder overlays"

for tool in fdtget fdtoverlay; do
    command -v "$tool" >/dev/null || die "$tool not found (install device-tree-compiler)"
done
[[ -f "$EXTLINUX" ]] || die "$EXTLINUX not found"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- the board ---------------------------------------------------------------------
if [[ -z "$COMPAT" ]]; then
    [[ -r /proc/device-tree/compatible ]] || die "no /proc/device-tree/compatible: pass --compatible"
    COMPAT="$(tr '\0' '\n' </proc/device-tree/compatible | head -n1)"
fi
[[ -n "$COMPAT" ]] || die "empty board compatible string"
log "board: $COMPAT"

# fdtget -t s prints a string list separated by spaces; compatible strings contain none.
dt_compat() { fdtget -t s "$1" / compatible 2>/dev/null || true; }
has_word() { local w; for w in $1; do [[ "$w" == "$2" ]] && return 0; done; return 1; }

BASE=""
shopt -s nullglob
for f in "$ROOT"/boot/dtb/*.dtb "$ROOT"/boot/*.dtb; do
    c="$(dt_compat "$f")"
    [[ "${c%% *}" == "$COMPAT" ]] || continue
    if ! fdtget "$f" /__symbols__ gpio >/dev/null 2>&1; then
        warn "$f matches the board but has no __symbols__; skipping it"
        continue
    fi
    BASE="$f"
    break
done
shopt -u nullglob

# --- the entry we want ---------------------------------------------------------------
# Prints the value of KEY in the entry LABEL of extlinux.conf (first occurrence,
# comment lines ignored).
entry_value() {
    awk -v want="$1" -v key="$2" '
        /^[[:space:]]*#/ { next }
        toupper($1) == "LABEL" { in_entry = ($2 == want); next }
        in_entry && toupper($1) == key {
            sub(/^[[:space:]]*[^[:space:]]+[[:space:]]+/, ""); print; exit
        }' "$EXTLINUX"
}
current_default() { awk 'toupper($1) == "DEFAULT" { print $2; exit }' "$EXTLINUX"; }

# The fallback (stock) entry: the DEFAULT we found, as remembered in our block.
FALLBACK="$(sed -n "s/^# fallback: \([^[:space:]]*\)$/\1/p" "$EXTLINUX" | head -n1)"
DEFAULT_NOW="$(current_default)"
FALLBACK="${FALLBACK:-primary}"
if [[ -n "$DEFAULT_NOW" && "$DEFAULT_NOW" != "$LABEL" && "$DEFAULT_NOW" != "$FALLBACK" ]]; then
    log "DEFAULT is '$DEFAULT_NOW' (set by someone else, e.g. jetson-io): leaving $EXTLINUX alone"
    exit 0
fi

WANT=1
REASON=""
if [[ -e "$DISABLED" || "${UAPF_OVERLAYS_DISABLE:-0}" == 1 ]]; then
    WANT=0; REASON="disabled by $DISABLED"
elif [[ -z "$BASE" ]]; then
    WANT=0; REASON="no kernel DTB with __symbols__ for $COMPAT in /boot/dtb or /boot"
elif [[ ! -f "$LIST" ]]; then
    WANT=0; REASON="no overlay list at $LIST"
fi

KEPT=()
SKIPPED=()
if [[ $WANT == 1 ]]; then
    log "base DTB: ${BASE#"$ROOT"}"
    cp "$BASE" "$TMP/merged.dtb"
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="$(printf '%s' "$line" | tr -d '[:space:]')"
        [[ -n "$line" ]] || continue
        ov="$ROOT$line"
        if [[ ! -f "$ov" ]]; then
            warn "overlay $line not found; skipped"; SKIPPED+=("$line:missing"); continue
        fi
        oc="$(dt_compat "$ov")"
        if [[ -n "$oc" ]] && ! has_word "$oc" "$COMPAT"; then
            warn "overlay $line is not for $COMPAT; skipped"; SKIPPED+=("$line:incompatible"); continue
        fi
        if fdtoverlay -i "$TMP/merged.dtb" -o "$TMP/next.dtb" "$ov" 2>"$TMP/err"; then
            mv "$TMP/next.dtb" "$TMP/merged.dtb"
            KEPT+=("$line")
            log "overlay ok: $line"
        else
            warn "overlay $line does not apply to ${BASE#"$ROOT"}: $(tr '\n' ' ' <"$TMP/err"); skipped"
            SKIPPED+=("$line:does-not-apply")
        fi
    done <"$LIST"
    if [[ ${#KEPT[@]} -eq 0 ]]; then WANT=0; REASON="no overlay applies"; fi
fi

# --- rewrite extlinux.conf ---------------------------------------------------------------
# Remove our block (between the markers), keep everything else byte for byte.
sed "/^$(printf '%s' "$BEGIN_MARK" | sed 's/[][\/.*^$]/\\&/g')\$/,/^$(printf '%s' "$END_MARK" | sed 's/[][\/.*^$]/\\&/g')\$/d" \
    "$EXTLINUX" >"$TMP/stripped"
# ...and the blank lines at its end (we add exactly one before our block).
sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$TMP/stripped"

if [[ $WANT == 1 ]]; then
    LINUX="$(entry_value "$FALLBACK" LINUX)"
    INITRD="$(entry_value "$FALLBACK" INITRD)"
    APPEND="$(entry_value "$FALLBACK" APPEND)"
    [[ -n "$LINUX" && -n "$APPEND" ]] || die "cannot read LINUX/APPEND of entry '$FALLBACK' in $EXTLINUX"
    OVERLAYS="$(IFS=,; printf '%s' "${KEPT[*]}")"
    {
        sed -E "s/^([[:space:]]*DEFAULT)[[:space:]].*/\1 $LABEL/I" "$TMP/stripped"
        printf '\n%s\n' "$BEGIN_MARK"
        printf '# fallback: %s\n' "$FALLBACK"
        printf 'LABEL %s\n' "$LABEL"
        printf '      MENU LABEL UAP Finder (GPS PPS + camera overlays; choose "%s" if this does not boot)\n' "$FALLBACK"
        printf '      LINUX %s\n' "$LINUX"
        printf '      FDT %s\n' "${BASE#"$ROOT"}"
        [[ -z "$INITRD" ]] || printf '      INITRD %s\n' "$INITRD"
        printf '      APPEND %s\n' "$APPEND"
        printf '      OVERLAYS %s\n' "$OVERLAYS"
        printf '%s\n' "$END_MARK"
    } >"$TMP/new"
    grep -qiE '^[[:space:]]*DEFAULT[[:space:]]' "$TMP/new" || sed -i "1i DEFAULT $LABEL" "$TMP/new"
else
    log "not using overlays: $REASON"
    sed -E "s/^([[:space:]]*DEFAULT)[[:space:]]+$LABEL\$/\1 $FALLBACK/I" "$TMP/stripped" >"$TMP/new"
fi

CHANGED=0
if ! cmp -s "$EXTLINUX" "$TMP/new"; then
    CHANGED=1
    if [[ $DRY_RUN == 1 ]]; then
        log "dry run: $EXTLINUX would become:"; cat "$TMP/new"
    else
        cp -p "$EXTLINUX" "$EXTLINUX.uapfinder.bak"
        cat "$TMP/new" >"$EXTLINUX.uapfinder.tmp"
        chmod --reference="$EXTLINUX" "$EXTLINUX.uapfinder.tmp"
        mv -f "$EXTLINUX.uapfinder.tmp" "$EXTLINUX"
        sync
        log "updated $EXTLINUX (previous version: $EXTLINUX.uapfinder.bak)"
    fi
else
    log "$EXTLINUX already up to date"
fi

# --- status for the setup page ---------------------------------------------------------------
if [[ -z "$ROOT" && $DRY_RUN == 0 ]]; then
    live=false
    [[ -d /proc/device-tree/pps-uapfinder ]] && live=true
    pps=""
    for d in /sys/class/pps/pps*; do
        [[ -e "$d/name" ]] || continue
        name="$(tr -dc 'A-Za-z0-9_.:@ -' <"$d/name")"
        pps+="${pps:+,}{\"device\":\"/dev/$(basename "$d")\",\"name\":\"$name\"}"
    done
    json_list() { local s="" x; for x in "$@"; do s+="${s:+,}\"${x//\"/}\""; done; printf '[%s]' "$s"; }
    install -d -m 0755 /run/uapfinder
    cat >/run/uapfinder/platform.json <<EOF
{"compatible":"$COMPAT","base_dtb":"${BASE#"$ROOT"}","overlays_enabled":$([[ $WANT == 1 ]] && echo true || echo false),"reason":"$REASON","overlays":$(json_list "${KEPT[@]}"),"skipped":$(json_list "${SKIPPED[@]}"),"pps_overlay_live":$live,"pps_devices":[$pps],"reboot_pending":$([[ $CHANGED == 1 ]] && echo true || echo false)}
EOF
fi

# --- reboot once so the new entry takes effect ---------------------------------------------------
if [[ $CHANGED == 1 && $REBOOT == 1 && $DRY_RUN == 0 && -z "$ROOT" ]]; then
    install -d -m 0755 "$STATE"
    stamp="$STATE/rebooted-for-$(sha256sum "$EXTLINUX" | cut -c1-16)"
    if [[ -e "$stamp" ]]; then
        warn "already rebooted once for this configuration; not rebooting again"
    else
        touch "$stamp"
        sync
        log "rebooting once to apply the device-tree overlays"
        systemctl --no-block reboot
    fi
fi
exit 0
