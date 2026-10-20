#!/usr/bin/env bash
# UAP Finder node first boot (uapfinder-firstboot.service, root). Installed as
# /opt/skynet/bin/uapfinder-firstboot by edge/deploy/install-node.sh.
#
# Once (stamp /var/lib/uapfinder/firstboot.done):
#   1. grow the root partition and its ext4 file system to the whole card
#      (growpart from cloud-guest-utils, then resize2fs online). If NVIDIA's own
#      first-boot resize already ran, growpart reports NOCHANGE and nothing happens.
#   2. host name uapfinder-XXXX (module serial number, else Ethernet MAC).
#   3. new SSH host keys (an image must never share them between nodes).
# Every boot:
#   4. a blank NVMe drive (no partition table, no file-system signature, unused:
#      see skynet_edge/appliance/firstboot.py) is partitioned, formatted ext4 with
#      label uapfinder-data and mounted at /var/lib/skynet; a drive with that label
#      is mounted there. A drive with anything else on it is never touched.
#
# NOT tested on Jetson hardware.
set -euo pipefail

STAMP_DIR=/var/lib/uapfinder
STAMP="$STAMP_DIR/firstboot.done"
PY=/opt/skynet/venv/bin/python3
DATA=/var/lib/skynet
LABEL=uapfinder-data

log() { printf 'uapfinder-firstboot: %s\n' "$*"; }
helper() { "$PY" -m skynet_edge.appliance.firstboot "$@"; }

grow_root() {
    local disk part dev fstype rc
    if ! read -r disk part dev < <(helper root-partition); then
        log "cannot find the root partition; not resizing"
        return 0
    fi
    fstype="$(findmnt -n -o FSTYPE /)"
    if ! command -v growpart >/dev/null; then
        log "growpart missing (package cloud-guest-utils); not resizing"
        return 0
    fi
    rc=0
    growpart "$disk" "$part" || rc=$?
    case $rc in
        0) log "grew $dev to the end of $disk" ;;
        1) log "$dev already fills $disk" ;;
        *) log "growpart failed ($rc); leaving the partition as it is"; return 0 ;;
    esac
    if [[ "$fstype" == ext4 || "$fstype" == ext3 ]]; then
        resize2fs "$dev" || log "resize2fs $dev failed"
    else
        log "root file system is $fstype; not resizing it"
    fi
}

set_hostname() {
    local suffix name
    suffix="$(helper suffix)"
    [[ "$suffix" =~ ^[0-9a-f]{4}$ ]] || suffix=0000
    name="uapfinder-$suffix"
    if [[ "$(hostname)" == "$name" ]]; then
        return 0
    fi
    hostnamectl set-hostname "$name" 2>/dev/null || { printf '%s\n' "$name" >/etc/hostname; hostname "$name"; }
    if grep -qE '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
        sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$name/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$name" >>/etc/hosts
    fi
    log "host name $name"
    systemctl try-restart avahi-daemon.service 2>/dev/null || true
}

regen_ssh_keys() {
    if command -v ssh-keygen >/dev/null && [[ -d /etc/ssh ]]; then
        rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
        ssh-keygen -A
        log "new SSH host keys"
    fi
}

mount_data() {
    # $1: partition carrying the uapfinder-data file system
    local part="$1" uuid tmp
    uuid="$(blkid -s UUID -o value "$part")"
    [[ -n "$uuid" ]] || { log "no UUID on $part"; return 0; }
    if ! grep -q "UUID=$uuid" /etc/fstab; then
        printf 'UUID=%s %s ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2\n' "$uuid" "$DATA" >>/etc/fstab
        log "added $part to /etc/fstab"
    fi
    if mountpoint -q "$DATA"; then
        return 0
    fi
    install -d -m 0750 "$DATA"
    # Keep whatever is already in /var/lib/skynet on the card (e.g. credentials).
    tmp="$(mktemp -d /run/uapfinder-data.XXXXXX)"
    mount "$part" "$tmp"
    cp -a --no-clobber "$DATA/." "$tmp/" 2>/dev/null || true
    umount "$tmp"
    rmdir "$tmp"
    mount "$DATA"
    log "mounted $part at $DATA"
}

setup_nvme() {
    local dev part existing why
    existing="$(blkid -L "$LABEL" 2>/dev/null || true)"
    if [[ -n "$existing" ]]; then
        mount_data "$existing"
    else
        for dev in /dev/nvme[0-9]n[0-9]; do
            [[ -b "$dev" ]] || continue
            if ! why="$(helper nvme-blank "$dev")"; then
                log "$dev not used: $why"
                continue
            fi
            log "$dev is blank: creating $LABEL"
            sgdisk --new=1:0:0 --typecode=1:8300 --change-name=1:"$LABEL" "$dev"
            partprobe "$dev" 2>/dev/null || true
            udevadm settle --timeout=20 || true
            part="${dev}p1"
            [[ -b "$part" ]] || { log "$part did not appear"; continue; }
            mkfs.ext4 -q -L "$LABEL" -m 1 "$part"
            mount_data "$part"
            break
        done
    fi
    if id skynet >/dev/null 2>&1 && [[ -d "$DATA" ]]; then
        chown skynet:skynet "$DATA"
        chmod 0750 "$DATA"
        install -d -m 0750 -o skynet -g skynet "$DATA/clips"
    fi
}

main() {
    if [[ ! -e "$STAMP" ]]; then
        grow_root
        set_hostname
        regen_ssh_keys
        install -d -m 0755 "$STAMP_DIR"
        date -u +%FT%TZ >"$STAMP"
    fi
    setup_nvme
}

main "$@"
