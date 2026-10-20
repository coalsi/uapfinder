#!/usr/bin/env bash
# Build the combined static web output: the public site at / and the live
# map at /map/, into site/dist. This is the Vercel build command, and works
# the same for any static host.
#
# Set UAPF_API_BASE to the server's public URL (for example the Mac Studio's
# Tailscale Funnel address). It fills PUBLIC_API_BASE (site) and
# VITE_API_BASE (map) unless those are already set individually.
#
# Without any API address the map is built in demo mode (clearly labelled
# simulated data), so a fresh deployment shows something useful; set
# VITE_DEMO=0 to build it against a same-origin API instead (the opt-in
# Vercel API, see scripts/enable-vercel-api.sh).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${UAPF_API_BASE:-}" ]]; then
  export PUBLIC_API_BASE="${PUBLIC_API_BASE:-$UAPF_API_BASE}"
  export VITE_API_BASE="${VITE_API_BASE:-$UAPF_API_BASE}"
fi
if [[ -z "${VITE_API_BASE:-}" && -z "${PUBLIC_API_BASE:-}" && ! -e "$root/api/index.py" ]]; then
  # No server anywhere: demo map, and the site doesn't call endpoints that
  # don't exist (its stats say "launching", its sign-up form "opening soon").
  export VITE_DEMO="${VITE_DEMO:-1}"
  export PUBLIC_API_ENABLED="${PUBLIC_API_ENABLED:-0}"
  echo "==> no API address set (UAPF_API_BASE): demo map, sign-ups shown as opening soon"
fi
# The map links back to the site, which is the same origin in this layout.
export VITE_SITE_URL="${VITE_SITE_URL:-/}"

echo "==> site"
(cd "$root/site" && npm ci --no-audit --no-fund && npm run build)

echo "==> map (base /map/)"
(cd "$root/dashboard" && npm ci --no-audit --no-fund && npm run build:map)

rm -rf "$root/site/dist/map"
cp -R "$root/dashboard/dist" "$root/site/dist/map"
echo "==> combined output: $root/site/dist (map at /map/)"
