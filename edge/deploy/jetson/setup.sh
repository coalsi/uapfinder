#!/usr/bin/env bash
# SKY-NET edge node provisioning for a Jetson Orin Nano on JetPack 6.x
# (Ubuntu 22.04, L4T r36). Idempotent: safe to re-run after a git pull to
# update the software; existing /etc/skynet/edge.yaml and keys are kept.
#
# NOT executed on real hardware by the authors: read it before running it.
#
# Usage (from a checkout of the repository, on the Jetson):
#   sudo SKYNET_NODE_ID=node-a SKYNET_NODE_KEY=<64 hex> SKYNET_SERVER_HOST=mac-studio \
#        TS_AUTHKEY=tskey-auth-... ./edge/deploy/jetson/setup.sh
#
# Environment (all optional):
#   SKYNET_NODE_ID       node id registered with the server (default: hostname)
#   SKYNET_NODE_KEY      64-hex node key from POST /api/v1/nodes (written to /etc/skynet/node.key)
#   SKYNET_SERVER_HOST   server host name/IP for a new config (default: mac-studio)
#   SKYNET_SERVER_HTTP   server HTTP base URL for a new config (default: http://$SKYNET_SERVER_HOST:8000)
#   SKYNET_GPS_TTY       GPS serial device name (default: ttyTHS1 = header pins 8/10 on JetPack 6)
#   SKYNET_GPS_BAUD      GPS baud rate (default: 9600)
#   SKYNET_PPS_DEVICE    PPS device, e.g. /dev/pps1 (default: first non-ktimer /dev/pps*)
#   TS_AUTHKEY           Tailscale auth key: install Tailscale and join the tailnet
#   SKYNET_TS_TAGS       tags to advertise (default: tag:skynet-node; must be in tagOwners or bound to the key)
#   SKYNET_NVPMODEL      nvpmodel mode id to set (list them with: nvpmodel -q --verbose)
#   SKYNET_CUDA=1        also install CuPy for the CUDA detection backend
#   SKYNET_SKIP_APT=1    skip apt-get (faster re-runs)
set -euo pipefail

log() { printf '\033[1;34m[skynet]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[skynet] WARNING:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[skynet] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
[[ -f "$REPO/edge/pyproject.toml" && -f "$REPO/common/pyproject.toml" ]] || die "cannot find the repository at $REPO"

NODE_ID="${SKYNET_NODE_ID:-$(hostname -s)}"
SERVER_HOST="${SKYNET_SERVER_HOST:-mac-studio}"
SERVER_HTTP="${SKYNET_SERVER_HTTP:-http://${SERVER_HOST}:8000}"
GPS_TTY="${SKYNET_GPS_TTY:-ttyTHS1}"
GPS_BAUD="${SKYNET_GPS_BAUD:-9600}"
TS_TAGS="${SKYNET_TS_TAGS:-tag:skynet-node}"
PREFIX=/opt/skynet
VENV="$PREFIX/venv"
ETC=/etc/skynet
STATE=/var/lib/skynet

[[ "$NODE_ID" =~ ^[A-Za-z0-9_.:-]{1,64}$ ]] || die "SKYNET_NODE_ID must be 1-64 chars of [A-Za-z0-9_.:-]"
if [[ -n "${SKYNET_NODE_KEY:-}" && ! "${SKYNET_NODE_KEY}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    die "SKYNET_NODE_KEY must be 64 hex characters"
fi

# --- 1. platform ---------------------------------------------------------------
if [[ -f /etc/nv_tegra_release ]]; then
    log "Jetson: $(head -n1 /etc/nv_tegra_release)"
    grep -q "R36" /etc/nv_tegra_release || warn "expected L4T R36 (JetPack 6.x); continuing anyway"
    IS_JETSON=1
else
    warn "/etc/nv_tegra_release not found: not a Jetson; Jetson-specific steps are skipped"
    IS_JETSON=0
fi

# --- 2. packages -----------------------------------------------------------------
if [[ "${SKYNET_SKIP_APT:-0}" != 1 ]]; then
    log "installing packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # gpsd: GPS daemon (NMEA/UBX -> JSON, time to chrony via shared memory);
    # chrony + pps-tools: GPS/PPS clock discipline; ffmpeg: clip encoding
    # (Orin Nano has no hardware encoder); gstreamer1.0-tools: gst-launch-1.0
    # fallback capture; i2c-tools: i2cdetect for the IMU.
    apt-get install -y --no-install-recommends \
        python3-venv python3-pip python3-numpy python3-yaml python3-requests \
        gpsd gpsd-clients chrony pps-tools ffmpeg gstreamer1.0-tools i2c-tools rsync curl ca-certificates
fi

if ! python3 -c "import cv2" 2>/dev/null; then
    log "OpenCV for Python not found; installing JetPack's (or Ubuntu's) build"
    apt-get install -y nvidia-opencv || apt-get install -y python3-opencv \
        || die "no OpenCV available: install the JetPack OpenCV component (sudo apt install nvidia-jetpack)"
fi

# --- 3. user and directories -------------------------------------------------------
if ! id skynet >/dev/null 2>&1; then
    log "creating system user skynet"
    useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --user-group skynet
fi
# Camera (video), GPU (render), IMU (i2c), GPS serial (dialout), GPIO (gpio): only groups that exist.
for g in video render i2c dialout gpio; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" skynet; fi
done
install -d -m 0755 "$PREFIX" "$ETC"
install -d -m 0750 -o skynet -g skynet "$STATE" "$STATE/clips"

# --- 4. software -------------------------------------------------------------------
log "installing skynet-edge into $VENV"
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude '*.egg-info' --exclude .pytest_cache \
    "$REPO/common" "$REPO/edge" "$PREFIX/src/"
# --system-site-packages: use JetPack's OpenCV (GStreamer-enabled, built against
# the system numpy 1.x) instead of a pip wheel that would shadow it.
[[ -x "$VENV/bin/python" ]] || python3 -m venv --system-site-packages "$VENV"
CONSTRAINTS="$PREFIX/constraints.txt"
echo "numpy<2" > "$CONSTRAINTS"  # the JetPack cv2 module fails to import under numpy 2.x
"$VENV/bin/pip" install --upgrade --quiet "pip>=23"
# skynet-common declares numpy>=1.24 but works with the system 1.21; --no-deps keeps pip
# from replacing the numpy that the JetPack OpenCV was built against.
"$VENV/bin/pip" install --quiet --no-deps "$PREFIX/src/common"
"$VENV/bin/pip" install --quiet -c "$CONSTRAINTS" "$PREFIX/src/edge[serial,imu]"
if [[ "${SKYNET_CUDA:-0}" == 1 ]]; then
    log "installing CuPy (CUDA detection backend; NOT hardware-tested)"
    "$VENV/bin/pip" install --quiet -c "$CONSTRAINTS" "cupy-cuda12x>=13" \
        || warn "CuPy install failed; the node will use the CPU backend"
fi
"$VENV/bin/python" - <<'EOF'
import re, cv2, numpy
info = cv2.getBuildInformation()
gst = re.search(r"GStreamer:\s+YES", info) is not None
print(f"[skynet] OpenCV {cv2.__version__} (GStreamer: {'YES' if gst else 'NO'}), numpy {numpy.__version__}")
if not gst:
    print("[skynet] OpenCV has no GStreamer: the node will capture through gst-launch-1.0 (camera.gstreamer_backend: auto)")
EOF

# --- 5. configuration -----------------------------------------------------------------
if [[ ! -f "$ETC/edge.yaml" ]]; then
    log "writing $ETC/edge.yaml from config/example.yaml (edit it: attitude, position, intrinsics)"
    sed -e "s/^  node_id: node-a /  node_id: ${NODE_ID} /" \
        -e "s/^  udp_host: mac-studio/  udp_host: ${SERVER_HOST}/" \
        -e "s#^  https_base_url: http://mac-studio:8000#  https_base_url: ${SERVER_HTTP}#" \
        "$REPO/edge/config/example.yaml" > "$ETC/edge.yaml"
    chmod 0644 "$ETC/edge.yaml"
else
    log "keeping existing $ETC/edge.yaml"
fi
if [[ -n "${SKYNET_NODE_KEY:-}" ]]; then
    install -m 0640 -o root -g skynet /dev/null "$ETC/node.key"
    printf '%s\n' "${SKYNET_NODE_KEY,,}" > "$ETC/node.key"
    log "node key written to $ETC/node.key"
fi
"$VENV/bin/python" -c "from skynet_edge.config import load_config; load_config('$ETC/edge.yaml')" \
    || die "$ETC/edge.yaml is invalid"

# --- 6. GPS: udev symlink and gpsd ---------------------------------------------------------
log "GPS on /dev/${GPS_TTY} -> /dev/gps0 at ${GPS_BAUD} baud"
sed -e "s/@GPS_TTY@/${GPS_TTY}/g" "$SCRIPT_DIR/99-skynet-gps.rules" > /etc/udev/rules.d/99-skynet-gps.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --subsystem-match=pps || true
sed -e "s/@GPS_BAUD@/${GPS_BAUD}/g" "$SCRIPT_DIR/gpsd.default" > /etc/default/gpsd
if [[ -e "/dev/${GPS_TTY}" ]]; then stty -F "/dev/${GPS_TTY}" "$GPS_BAUD" || true; fi
systemctl enable gpsd.socket gpsd.service >/dev/null
systemctl restart gpsd.service || warn "gpsd did not start (is the GPS connected?)"

# --- 7. time: chrony with the GPS PPS ------------------------------------------------------
PPS="${SKYNET_PPS_DEVICE:-}"
if [[ -z "$PPS" ]]; then
    for d in /sys/class/pps/pps*; do
        [[ -e "$d/name" ]] || continue
        # pps0 on Jetson kernels is often the "ktimer" test source, not a real pulse.
        if [[ "$(cat "$d/name")" != ktimer ]]; then PPS="/dev/$(basename "$d")"; break; fi
    done
fi
install -d /etc/chrony/conf.d
if [[ -n "$PPS" ]]; then
    log "chrony: PPS refclock on $PPS, numbered by gpsd's NMEA time (SHM 0)"
    sed -e "s#@PPS_DEVICE@#${PPS}#g" "$SCRIPT_DIR/chrony-gps-pps.conf" > /etc/chrony/conf.d/skynet-gps.conf
else
    warn "no PPS device found: clock discipline falls back to network NTP (good to a few ms at best)."
    warn "Wire the GPS PPS output to a GPIO with a pps-gpio device-tree overlay (README) and re-run."
    cp "$SCRIPT_DIR/chrony-gps-nopps.conf" /etc/chrony/conf.d/skynet-gps.conf
fi
grep -qE '^\s*confdir\s+/etc/chrony/conf.d' /etc/chrony/chrony.conf \
    || grep -qF 'include /etc/chrony/conf.d/*.conf' /etc/chrony/chrony.conf \
    || echo 'include /etc/chrony/conf.d/*.conf' >> /etc/chrony/chrony.conf
systemctl enable chrony >/dev/null
systemctl restart chrony

# --- 8. service ------------------------------------------------------------------------------
log "installing systemd unit"
install -m 0644 "$REPO/edge/deploy/systemd/skynet-edge.service" /etc/systemd/system/skynet-edge.service
systemctl daemon-reload
systemctl enable skynet-edge.service >/dev/null

# --- 9. optional: Tailscale --------------------------------------------------------------------
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    if ! command -v tailscale >/dev/null; then
        log "installing Tailscale (official install script)"
        curl -fsSL https://tailscale.com/install.sh | sh
    fi
    systemctl enable --now tailscaled >/dev/null
    if tailscale status >/dev/null 2>&1; then
        log "Tailscale already up: $(tailscale ip -4 2>/dev/null | head -n1)"
    else
        log "joining the tailnet as ${NODE_ID} with tags ${TS_TAGS}"
        KEYFILE="$(mktemp)"
        chmod 0600 "$KEYFILE"
        printf '%s' "$TS_AUTHKEY" > "$KEYFILE"
        tailscale up --auth-key="file:${KEYFILE}" --hostname="${NODE_ID}" --advertise-tags="${TS_TAGS}" \
            --timeout=60s || { rm -f "$KEYFILE"; die "tailscale up failed"; }
        rm -f "$KEYFILE"
    fi
fi

# --- 10. optional: power mode ---------------------------------------------------------------------
if [[ -n "${SKYNET_NVPMODEL:-}" ]]; then
    if [[ $IS_JETSON == 1 ]] && command -v nvpmodel >/dev/null; then
        log "setting nvpmodel mode ${SKYNET_NVPMODEL} (persists across reboots)"
        nvpmodel -m "${SKYNET_NVPMODEL}" || warn "nvpmodel -m ${SKYNET_NVPMODEL} failed (see nvpmodel -q --verbose)"
        nvpmodel -q || true
    else
        warn "SKYNET_NVPMODEL set but nvpmodel is not available"
    fi
fi

# --- 11. start ------------------------------------------------------------------------------------------
if [[ -f "$ETC/node.key" ]] || grep -qE '^\s*key_hex:' "$ETC/edge.yaml"; then
    systemctl restart skynet-edge.service
    log "skynet-edge started: journalctl -u skynet-edge -f"
else
    warn "no node key yet: put it in $ETC/node.key (mode 0640, group skynet), then: systemctl start skynet-edge"
fi

cat <<EOF

Next steps (README "Calibration" and "Validation on hardware"):
  1. Edit $ETC/edge.yaml: attitude (solve from a star field), gps.static (surveyed, ellipsoid height),
     camera intrinsics (checkerboard), and the sky mask.
  2. Check time:   chronyc sources -v   (PPS should be '*' with offsets of microseconds)
     Check GPS:    gpspipe -w -n 10 | grep TPV
     Check PPS:    sudo ppstest ${PPS:-/dev/ppsN}
  3. Benchmark:    sudo -u skynet $VENV/bin/skynet-edge bench    (and watch: sudo tegrastats)
  4. Service:      systemctl status skynet-edge; journalctl -u skynet-edge -f
EOF
