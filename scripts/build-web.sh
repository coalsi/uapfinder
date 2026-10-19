#!/usr/bin/env bash
# Build the combined static web output: the public site at / and the live
# map at /map/, into site/dist. This is the Vercel build command, and works
# the same for any static host.
#
# Set UAPF_API_BASE to the server's public URL (for example the Mac Studio's
# Tailscale Funnel address) when the API is not served from the same origin.
# It fills PUBLIC_API_BASE (site) and VITE_API_BASE (map) unless those are
# already set individually.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${UAPF_API_BASE:-}" ]]; then
  export PUBLIC_API_BASE="${PUBLIC_API_BASE:-$UAPF_API_BASE}"
  export VITE_API_BASE="${VITE_API_BASE:-$UAPF_API_BASE}"
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
