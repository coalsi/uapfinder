#!/usr/bin/env bash
# SKY-NET / UAP Finder node setup on a Jetson that already runs JetPack 6.x
# (Ubuntu 22.04, L4T r36). A thin wrapper around ../install-node.sh, which
# installs exactly what the flashable image contains; afterwards the node is
# enrolled from its setup page (http://uapfinder.local) with a claim code.
#
# People who already have a node id and key (POST /api/v1/nodes) can pass them
# here instead; they are written to /var/lib/skynet/credentials.json, the same
# file enrollment writes. Idempotent: safe to re-run after a git pull.
#
# NOT executed on real hardware by the authors: read it before running it.
#
# Usage (from a checkout of the repository, on the Jetson):
#   sudo ./edge/deploy/jetson/setup.sh                        # then enroll at http://uapfinder.local
#   sudo SKYNET_NODE_ID=node-a SKYNET_NODE_KEY=<64 hex> SKYNET_SERVER_HOST=mac-studio \
#        TS_AUTHKEY=tskey-auth-... ./edge/deploy/jetson/setup.sh
#
# Environment (all optional):
#   UAPF_ENROLL_URL      enrollment server for claim codes (written to /etc/skynet/defaults.yaml)
#   SKYNET_NODE_ID       node id registered with the server (default: hostname; only with SKYNET_NODE_KEY)
#   SKYNET_NODE_KEY      64-hex node key from POST /api/v1/nodes
#   SKYNET_SERVER_HOST   server host name/IP (default: mac-studio)
#   SKYNET_SERVER_HTTP   server HTTP base URL (default: http://$SKYNET_SERVER_HOST:8000)
#   SKYNET_SERVER_PORT   server UDP port (default: 9999)
#   SKYNET_GPS_TTY       GPS serial device name (default: ttyTHS1 = header pins 8/10 on JetPack 6)
#   SKYNET_GPS_BAUD      GPS baud rate (default: 9600)
#   SKYNET_PPS_DEVICE    PPS device, e.g. /dev/pps1 (default: detected at every start)
#   TS_AUTHKEY           Tailscale auth key: join the tailnet (with SKYNET_NODE_KEY: at node start)
#   SKYNET_TS_TAGS       tags to advertise (default: tag:skynet-node; must be in tagOwners or bound to the key)
#   SKYNET_NVPMODEL      nvpmodel mode id to set (list them with: nvpmodel -q --verbose)
#   SKYNET_CUDA=1        also install CuPy for the CUDA detection backend
#   SKYNET_SKIP_APT=1    skip apt-get (faster re-runs)
set -euo pipefail

log() { printf '\033[1;34m[skynet]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[skynet] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NODE_ID="${SKYNET_NODE_ID:-$(hostname -s)}"
SERVER_HOST="${SKYNET_SERVER_HOST:-mac-studio}"
SERVER_HTTP="${SKYNET_SERVER_HTTP:-http://${SERVER_HOST}:8000}"
SERVER_PORT="${SKYNET_SERVER_PORT:-9999}"
TS_TAGS="${SKYNET_TS_TAGS:-tag:skynet-node}"
if [[ -n "${SKYNET_NODE_KEY:-}" ]]; then
    [[ "$NODE_ID" =~ ^[A-Za-z0-9_.:-]{1,64}$ ]] || die "SKYNET_NODE_ID must be 1-64 chars of [A-Za-z0-9_.:-]"
    [[ "${SKYNET_NODE_KEY}" =~ ^[0-9a-fA-F]{64}$ ]] || die "SKYNET_NODE_KEY must be 64 hex characters"
fi

"$SCRIPT_DIR/../install-node.sh"

VENV=/opt/skynet/venv
if [[ -n "${SKYNET_NODE_KEY:-}" ]]; then
    log "writing /var/lib/skynet/credentials.json for node ${NODE_ID}"
    # Secrets go through the environment, never the command line.
    NODE_ID="$NODE_ID" NODE_KEY="$SKYNET_NODE_KEY" SERVER_HOST="$SERVER_HOST" SERVER_HTTP="$SERVER_HTTP" \
    SERVER_PORT="$SERVER_PORT" TS_AUTHKEY="${TS_AUTHKEY:-}" TS_TAGS="$TS_TAGS" "$VENV/bin/python" - <<'EOF'
import os, time
from skynet_edge.appliance.credentials import save_credentials, validate_enrollment
from skynet_edge.appliance.paths import Paths
e = os.environ
resp = {"node_id": e["NODE_ID"], "node_key": e["NODE_KEY"].lower(), "transport": "udp",
        "udp_host": e["SERVER_HOST"], "udp_port": int(e["SERVER_PORT"]), "https_base_url": e["SERVER_HTTP"],
        "tailscale": None}
if e.get("TS_AUTHKEY"):
    resp["tailscale"] = {"auth_key": e["TS_AUTHKEY"], "tags": [t for t in e["TS_TAGS"].split(",") if t]}
data = validate_enrollment(resp)
data.update(enroll_url="manual", enrolled_ns=time.time_ns())
save_credentials(Paths().credentials, data)
EOF
    chown skynet:skynet /var/lib/skynet/credentials.json
    systemctl restart skynet-edge.service 2>/dev/null || true
elif [[ -n "${TS_AUTHKEY:-}" ]]; then
    systemctl enable --now tailscaled >/dev/null 2>&1 || true
    if tailscale status >/dev/null 2>&1; then
        log "Tailscale already up: $(tailscale ip -4 2>/dev/null | head -n1)"
    else
        log "joining the tailnet as $(hostname -s) with tags ${TS_TAGS}"
        KEYDIR="$(mktemp -d)"
        chmod 0700 "$KEYDIR"
        printf '%s' "$TS_AUTHKEY" >"$KEYDIR/key"
        tailscale up --auth-key="file:${KEYDIR}/key" --hostname="$(hostname -s)" --advertise-tags="${TS_TAGS}" \
            --timeout=60s || { rm -rf "$KEYDIR"; die "tailscale up failed"; }
        rm -rf "$KEYDIR"
    fi
fi

cat <<EOF

Next steps:
  1. Open http://uapfinder.local (or http://$(hostname -s).local) on a phone or laptop on the same network:
     enter the claim code (unless you passed SKYNET_NODE_KEY), set a device password, aim the camera.
  2. Hand edits go in /etc/skynet/edge.local.yaml (it overrides the generated /etc/skynet/edge.yaml).
  3. Check:  journalctl -u skynet-edge -f;  chronyc sources -v;  gpspipe -w -n 10 | grep TPV
EOF
