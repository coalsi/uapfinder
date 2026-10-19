#!/usr/bin/env bash
# Switch the Vercel project from static-only (site + map, the default) to
# static + the Python API as a Vercel Function ("vercel mode" in
# server/README.md). Only needed when the API runs on Vercel instead of a
# standalone server such as the Mac Studio. Commit the result, then set the
# SKYNET_* environment variables in the Vercel project.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/deploy/vercel-api"

cp -R "$src/api" "$root/"
cp "$src/requirements.txt" "$src/.python-version" "$root/"
cp "$src/vercel.json" "$root/vercel.json"

cat <<EOF
Copied api/index.py, requirements.txt, .python-version and the API vercel.json
to the repository root. Next:
  git add api requirements.txt .python-version vercel.json && git commit -m "Enable the Vercel API"
and set SKYNET_MASTER_KEY, SKYNET_ADMIN_TOKEN, Redis and Postgres variables in
Vercel (see server/README.md, "Deploy on Vercel").
EOF
