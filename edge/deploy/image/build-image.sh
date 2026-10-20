#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Build the flashable UAP Finder node image for the Jetson Orin Nano
# developer kit (microSD card), from NVIDIA's Jetson Linux R36.5.2 BSP.
#
#   sudo edge/deploy/image/build-image.sh --version 0.2.0 --enroll-url https://example.org \
#        [--work /mnt/uapf/work] [--cache /mnt/uapf/cache] [--out /mnt/uapf/out] \
#        [--rootfs-cache DIR] [--from STEP] [--only-through STEP] [--keep-raw]
#        [--allow-empty-enroll-url]
#
# Host: x86_64 Ubuntu 22.04 (what NVIDIA's flashing tools support), root,
# about 40 GB free in --work, network access to NVIDIA, Ubuntu, Tailscale and
# GitHub. Output in --out:
#   uapfinder-node-<version>-orin-nano.img.xz   and   SHA256SUMS
#
# Steps (each logged; a finished step leaves a stamp in <work>/.stamps and
# is skipped on the next run; --from STEP re-runs from STEP on; --only-through
# STEP stops after STEP; --rootfs-cache keeps the built minimal rootfs apart
# from the downloads, default: the --cache directory):
#   host        host packages, qemu-user-static binfmt check
#   download    BSP (and sample rootfs if ROOTFS_SOURCE=sample) into --cache, verified
#   extract     Linux_for_Tegra/ from the BSP; NVIDIA's flashing prerequisites
#   rootfs      NVIDIA's minimal Ubuntu 22.04 rootfs (nv_build_samplefs.sh, cached) into rootfs/
#   binaries    apply_binaries.sh (NVIDIA's L4T packages and apt source)
#   overlays    the device-tree overlays must apply to the BSP's devkit DTBs
#   user        l4t_create_default_user.sh (skips NVIDIA's first-boot wizard)
#   customize   customize_rootfs.sh (packages, node software, platform, defaults, clean-up)
#   checkroot   test-image.sh on the root file system
#   image       jetson-disk-image-creator.sh -d SD
#   checkimage  test-image.sh on the image
#   compress    xz -T0 -9, size limit, SHA256SUMS
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
# shellcheck source=versions.env
. "$SCRIPT_DIR/versions.env"

STEPS=(host download extract rootfs binaries overlays user customize checkroot image checkimage compress)
VERSION="" ENROLL_URL="" WORK=/mnt/uapf/work CACHE=/mnt/uapf/cache OUT=/mnt/uapf/out
FROM="" THROUGH="" KEEP_RAW=0 ALLOW_EMPTY_URL=0 ROOTFS_CACHE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="${2:?}"; shift 2 ;;
        --enroll-url) ENROLL_URL="${2-}"; shift 2 ;;
        --work) WORK="${2:?}"; shift 2 ;;
        --cache) CACHE="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --from) FROM="${2:?}"; shift 2 ;;
        --only-through) THROUGH="${2:?}"; shift 2 ;;
        --rootfs-cache) ROOTFS_CACHE="${2:?}"; shift 2 ;;
        --keep-raw) KEEP_RAW=1; shift ;;
        --allow-empty-enroll-url) ALLOW_EMPTY_URL=1; shift ;;
        -h | --help) sed -n '3,32p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
require_root
[[ "$(uname -m)" == x86_64 ]] || die "build on an x86_64 host (NVIDIA's flashing tools are x86_64 binaries)"
[[ -n "$VERSION" ]] || die "--version is required"
[[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+_-]*$ ]] || die "bad version '$VERSION' (letters, digits, . + _ -)"
if [[ -z "$ENROLL_URL" && $ALLOW_EMPTY_URL == 0 ]]; then
    die "no enroll URL: pass --enroll-url (the workflow takes it from the input or the repository variable UAPF_ENROLL_URL)"
fi
for s in "$FROM" "$THROUGH"; do
    [[ -z "$s" ]] || printf '%s\n' "${STEPS[@]}" | grep -qx -- "$s" || die "unknown step '$s' (${STEPS[*]})"
done
# shellcheck source=/dev/null
os="$(. /etc/os-release && echo "${ID:-} ${VERSION_ID:-}")"
[[ "$os" == "ubuntu 22.04" ]] || warn "NVIDIA supports Ubuntu 22.04 (or 20.04) hosts for R36; this is '$os'"

NAME="uapfinder-node-${VERSION}-orin-nano"
ROOTFS_CACHE="${ROOTFS_CACHE:-$CACHE}"
install -d "$WORK" "$CACHE" "$ROOTFS_CACHE" "$OUT" "$WORK/.stamps"
WORK="$(cd "$WORK" && pwd)"; CACHE="$(cd "$CACHE" && pwd)"; OUT="$(cd "$OUT" && pwd)"
ROOTFS_CACHE="$(cd "$ROOTFS_CACHE" && pwd)"
L4T="$WORK/Linux_for_Tegra"
ROOTFS="$L4T/rootfs"
IMG="$WORK/$NAME.img"
log "UAP Finder node image $VERSION on Jetson Linux R$L4T_VERSION (JetPack $JETPACK_VERSION); enroll_url=${ENROLL_URL:-<none>}"
log "work=$WORK cache=$CACHE out=$OUT"

# Stamps: a re-run skips finished steps; --from removes the stamps from that step on.
if [[ -n "$FROM" ]]; then
    drop=0
    for s in "${STEPS[@]}"; do
        [[ "$s" == "$FROM" ]] && drop=1
        [[ $drop == 1 ]] && rm -f "$WORK/.stamps/$s"
    done
fi
# The image depends on the version and enroll URL: if they changed, redo customize and later.
PARAMS="$VERSION|$ENROLL_URL|$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo nogit)"
if [[ -f "$WORK/.stamps/params" && "$(cat "$WORK/.stamps/params")" != "$PARAMS" ]]; then
    warn "version, enroll URL or commit changed since the last run: rebuilding from 'extract'"
    for s in extract rootfs binaries overlays user customize checkroot image checkimage compress; do rm -f "$WORK/.stamps/$s"; done
fi
printf '%s' "$PARAMS" >"$WORK/.stamps/params"

done_step() { [[ -f "$WORK/.stamps/$1" ]]; }
mark_step() { date -u +%FT%TZ >"$WORK/.stamps/$1"; }
run_step() {
    local s="$1"
    if done_step "$s"; then log "step $s: already done (stamp $WORK/.stamps/$s)"; return 0; fi
    step "step $s"
    "step_$s"
    mark_step "$s"
}

# --- host ---------------------------------------------------------------------------------
step_host() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends qemu-user-static binfmt-support device-tree-compiler \
        xz-utils lbzip2 bzip2 curl ca-certificates git sudo rsync python3 gdisk parted e2fsprogs \
        dosfstools util-linux kmod file
    if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
        mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
        update-binfmts --enable qemu-aarch64 2>/dev/null || true
    fi
    if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
        for f in /usr/lib/binfmt.d/qemu-aarch64*.conf; do
            [[ -f "$f" ]] && cat "$f" >/proc/sys/fs/binfmt_misc/register && break
        done
    fi
    require_binfmt
    log "binfmt: $(head -n2 /proc/sys/fs/binfmt_misc/qemu-aarch64 | tr '\n' ' ')"
    local avail
    avail="$(df --output=avail -B1G "$WORK" | tail -n1 | tr -d ' ')"
    log "free space in $WORK: ${avail} GiB"
    [[ "$avail" -ge 30 ]] || die "need at least 30 GiB free in $WORK (have $avail)"
}

# --- download -------------------------------------------------------------------------------
# fetch DEST SHA256 SHA1 NAME... : try every base URL with every file name.
fetch() {
    local dest="$1" sha256="$2" sha1="$3"; shift 3
    if [[ -f "$dest" ]] && echo "$sha256  $dest" | sha256sum -c --quiet - 2>/dev/null; then
        log "cached: $(basename "$dest") (SHA-256 ok)"
        return 0
    fi
    local base name url
    for base in $L4T_URL_BASES; do
        for name in "$@"; do
            url="$base/$name"
            log "downloading $url"
            if curl -fL --retry 3 --retry-delay 5 --connect-timeout 30 -o "$dest.partial" "$url"; then
                local got
                got="$(sha256sum "$dest.partial" | cut -d' ' -f1)"
                [[ "$got" == "$sha256" ]] || die "$url: SHA-256 $got does not match the pin $sha256 (versions.env)"
                got="$(sha1sum "$dest.partial" | cut -d' ' -f1)"
                [[ "$got" == "$sha1" ]] || die "$url: SHA-1 $got does not match NVIDIA's published $sha1 (versions.env)"
                mv -f "$dest.partial" "$dest"
                log "verified $(basename "$dest") (SHA-256 and NVIDIA SHA-1)"
                return 0
            fi
            rm -f "$dest.partial"
        done
    done
    die "could not download any of: $* (from $L4T_URL_BASES)"
}
BSP_TAR="$CACHE/Jetson_Linux_R${L4T_VERSION}_aarch64.tbz2"
SAMPLE_TAR="$CACHE/Tegra_Linux_Sample-Root-Filesystem_R${L4T_VERSION}_aarch64.tbz2"
MINIMAL_TAR="$ROOTFS_CACHE/rootfs-minimal-${UBUNTU_CODENAME}-R${L4T_VERSION}.tbz2"
step_download() {
    # shellcheck disable=SC2086  # word lists from versions.env
    fetch "$BSP_TAR" "$BSP_SHA256" "$BSP_SHA1" $BSP_NAMES
    if [[ "$ROOTFS_SOURCE" == sample ]]; then
        # shellcheck disable=SC2086
        fetch "$SAMPLE_TAR" "$ROOTFS_SHA256" "$ROOTFS_SHA1" $ROOTFS_NAMES
    fi
    # NVIDIA's SHA-1 list, for the log (the pins above are what is enforced).
    local base
    for base in $L4T_URL_BASES; do
        if curl -fsSL --retry 2 -o "$CACHE/$SHA_LIST_NAME" "$base/$SHA_LIST_NAME"; then
            log "NVIDIA $SHA_LIST_NAME:"; grep -iE 'Jetson_Linux|Sample-Root' "$CACHE/$SHA_LIST_NAME" || true
            grep -qi "$BSP_SHA1" "$CACHE/$SHA_LIST_NAME" || die "NVIDIA's $SHA_LIST_NAME does not list the pinned BSP SHA-1 $BSP_SHA1"
            break
        fi
    done
}

# --- extract ----------------------------------------------------------------------------------
step_extract() {
    if [[ -d "$L4T" ]]; then
        log "removing the previous $L4T"
        chroot_leave_all
        rm -rf "$L4T"
    fi
    tar -I lbzip2 -xpf "$BSP_TAR" -C "$WORK"
    [[ -x "$L4T/apply_binaries.sh" && -x "$L4T/tools/jetson-disk-image-creator.sh" ]] \
        || die "unexpected BSP layout: no apply_binaries.sh / tools/jetson-disk-image-creator.sh"
    log "NVIDIA host prerequisites (tools/l4t_flash_prerequisites.sh)"
    "$L4T/tools/l4t_flash_prerequisites.sh"
}
# Unmount anything a failed earlier run left mounted under the work tree.
chroot_leave_all() {
    local m
    while read -r m; do umount -l "$m" 2>/dev/null || true; done < <(findmnt -rn -o TARGET | grep -F "$L4T/" | sort -r)
}

# --- rootfs -------------------------------------------------------------------------------------
step_rootfs() {
    find "$ROOTFS" -mindepth 1 -maxdepth 1 ! -name README.txt -exec rm -rf {} + 2>/dev/null || true
    if [[ "$ROOTFS_SOURCE" == sample ]]; then
        log "NVIDIA's sample rootfs (desktop; purged later)"
        tar -I lbzip2 -xpf "$SAMPLE_TAR" -C "$ROOTFS" --numeric-owner
        return 0
    fi
    if [[ -f "$MINIMAL_TAR" && -f "$MINIMAL_TAR.sha256" ]] && (cd "$ROOTFS_CACHE" && sha256sum -c --quiet "$(basename "$MINIMAL_TAR").sha256"); then
        log "using the cached minimal rootfs $(basename "$MINIMAL_TAR")"
    else
        local sf="$L4T/tools/samplefs"
        [[ -x "$sf/nv_build_samplefs.sh" ]] || die "$sf/nv_build_samplefs.sh not found"
        [[ -f "$sf/nvubuntu-${UBUNTU_CODENAME}-minimal-aarch64-packages" ]] \
            || warn "no nvubuntu-${UBUNTU_CODENAME}-minimal-aarch64-packages in $sf: nv_build_samplefs.sh may refuse"
        log "building NVIDIA's minimal ${UBUNTU_CODENAME} rootfs (nv_build_samplefs.sh; under qemu this takes about an hour)"
        rm -f "$sf/sample_fs.tbz2"
        (cd "$sf" && ./nv_build_samplefs.sh --abi aarch64 --distro ubuntu --flavor minimal --version "$UBUNTU_CODENAME")
        [[ -f "$sf/sample_fs.tbz2" ]] || die "nv_build_samplefs.sh wrote no sample_fs.tbz2"
        mv -f "$sf/sample_fs.tbz2" "$MINIMAL_TAR"
        (cd "$ROOTFS_CACHE" && sha256sum "$(basename "$MINIMAL_TAR")" >"$(basename "$MINIMAL_TAR").sha256")
    fi
    tar -I lbzip2 -xpf "$MINIMAL_TAR" -C "$ROOTFS" --numeric-owner
    [[ -x "$ROOTFS/bin/bash" ]] || die "the minimal rootfs has no /bin/bash"
}

# --- binaries -----------------------------------------------------------------------------------
step_binaries() {
    (cd "$L4T" && ./apply_binaries.sh)
    [[ -f "$ROOTFS/etc/nv_tegra_release" ]] || die "apply_binaries.sh did not write etc/nv_tegra_release"
    log "rootfs: $(head -n1 "$ROOTFS/etc/nv_tegra_release")"
    grep -q "R${L4T_VERSION%%.*} (release), REVISION: ${L4T_VERSION#*.}" "$ROOTFS/etc/nv_tegra_release" \
        || warn "etc/nv_tegra_release does not say R${L4T_VERSION%%.*} REVISION ${L4T_VERSION#*.}"
}

step_overlays() { "$REPO/edge/deploy/platform/check-overlays.sh" "$L4T"; }

# --- user -------------------------------------------------------------------------------------------
step_user() {
    if grep -q "^${DEFAULT_USER}:" "$ROOTFS/etc/passwd"; then
        log "default user $DEFAULT_USER already exists"
        return 0
    fi
    # A throw-away password: customize_rootfs.sh locks the account; the setup page sets the real one.
    local pw
    pw="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
    (cd "$L4T" && ./tools/l4t_create_default_user.sh -u "$DEFAULT_USER" -p "$pw" -n "$DEFAULT_HOSTNAME" --accept-license)
    grep -q "^${DEFAULT_USER}:" "$ROOTFS/etc/passwd" || die "l4t_create_default_user.sh did not create $DEFAULT_USER"
}

step_customize() {
    "$SCRIPT_DIR/customize_rootfs.sh" --rootfs "$ROOTFS" --repo "$REPO" --version "$VERSION" --enroll-url "$ENROLL_URL"
}

TEST_ARGS=(--expect-version "$VERSION")
[[ -z "$ENROLL_URL" ]] || TEST_ARGS+=(--expect-enroll-url "$ENROLL_URL")
step_checkroot() { "$SCRIPT_DIR/test-image.sh" --rootfs "$ROOTFS" "${TEST_ARGS[@]}"; }

# --- image ----------------------------------------------------------------------------------------------
step_image() {
    local creator="$L4T/tools/jetson-disk-image-creator.sh" board args
    if grep -q -- "${BOARD_PREFERRED})" "$creator"; then
        board="$BOARD_PREFERRED"
    else
        board="$BOARD_FALLBACK"
        warn "$(basename "$creator") does not know $BOARD_PREFERRED: using $board (no MAXN SUPER power modes)"
    fi
    args=(-o "$IMG" -b "$board")
    if grep -qE -- '-d \| --device|-d <device>' "$creator"; then args+=(-d SD); fi
    rm -f "$IMG"
    log "jetson-disk-image-creator.sh ${args[*]}"
    (cd "$L4T/tools" && ./jetson-disk-image-creator.sh "${args[@]}")
    [[ -s "$IMG" ]] || die "jetson-disk-image-creator.sh wrote no $IMG"
    log "raw image: $(du -h --apparent-size "$IMG" | cut -f1) apparent, $(du -h "$IMG" | cut -f1) allocated"
}
step_checkimage() { "$SCRIPT_DIR/test-image.sh" "$IMG" "${TEST_ARGS[@]}"; }

step_compress() {
    local xz="$OUT/$NAME.img.xz"
    rm -f "$xz" "$OUT/SHA256SUMS"
    log "xz -T0 -9 (this takes a while)"
    xz -T0 -9 -c "$IMG" >"$xz.partial"
    mv -f "$xz.partial" "$xz"
    local size
    size="$(stat -c %s "$xz")"
    log "compressed image: $size bytes ($(numfmt --to=iec "$size"))"
    if [[ "$size" -ge "$MAX_RELEASE_BYTES" ]]; then
        die "the compressed image is $(numfmt --to=iec "$size"), not under GitHub's 2 GiB release-file limit; slim the rootfs (README.md, 'Image size')"
    fi
    (cd "$OUT" && sha256sum "$NAME.img.xz" >SHA256SUMS)
    cat "$OUT/SHA256SUMS"
    if [[ $KEEP_RAW == 0 ]]; then rm -f "$IMG"; fi
}

on_exit() {
    local rc=$?
    chroot_leave_all
    if [[ $rc != 0 ]]; then
        printf '\033[1;31m[image] build failed (exit %s). Re-run the same command to continue; --from STEP redoes a step.\033[0m\n' "$rc" >&2
    fi
}
trap on_exit EXIT
for s in "${STEPS[@]}"; do
    run_step "$s"
    if [[ "$s" == "$THROUGH" ]]; then log "stopping after step $s (--only-through)"; exit 0; fi
done
log "done: $OUT/$NAME.img.xz"
