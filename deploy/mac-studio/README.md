# Run the UAP Finder server on a Mac Studio

This is the deployment the project is planned around: a Mac Studio at home
runs the real-time server (UDP ingest, matching, tracks, API, database), edge
nodes reach it privately over **Tailscale**, and the public website and live
map on **Vercel** reach its API through **Tailscale Funnel**.

```
Jetson nodes ──UDP 9999 + HTTP 8000 over Tailscale──▶ Mac Studio
                                                      server + PostgreSQL/PostGIS
Browser ◀── site + /map/ from Vercel ── API/WebSocket via Funnel (public HTTPS) ──┘
```

Nothing here opens a port on your router or exposes your home IP address:
nodes connect through the tailnet, and the public only ever sees the
`*.ts.net` Funnel address.

> Written from the projects' documentation and source code; the macOS,
> Tailscale and Funnel steps have not been run end to end on a Mac. The
> server itself runs natively on macOS (no Linux-only calls) and passes its
> test suite on Linux.

## 1. Keep the Mac awake and recoverable

```bash
sudo pmset -a sleep 0 disksleep 0     # never sleep (the display can still turn off)
sudo pmset -a autorestart 1           # power back on after a power failure
```

Also turn on **System Settings → Energy → "Start up automatically after a
power failure"** (the wording differs between macOS versions), and test it
once by pulling the plug: reports differ by model and macOS version.

After an unattended reboot the Mac must get as far as running the server
and Tailscale without you:

* The server runs as a LaunchDaemon (below), so it starts at boot without
  anyone logging in.
* **FileVault**: on Apple silicon a FileVault-encrypted Mac waits at the
  unlock screen after a reboot, and nothing starts until someone logs in.
  Either accept that (log in after an outage) or turn FileVault off on this
  machine. That is a security trade-off for you to make; the server's data
  (volunteer emails, clips) lives on this disk.
* **Tailscale**: the recommended **Standalone** app (and the App Store app)
  only connect while a user is logged in. For unattended restarts either
  enable automatic login for a dedicated user (not possible with FileVault
  on), or use the open-source `tailscaled` daemon, which runs without a
  login but which Tailscale says gets less attention and does not recommend
  for production. See Tailscale's [macOS variants](https://tailscale.com/kb/1065/macos-variants).

## 2. Install the software

```bash
# Homebrew (https://brew.sh), then:
brew install python@3.13 postgresql@17 postgis
git clone https://github.com/coalsi/uapfinder.git ~/uapfinder
cd ~/uapfinder
python3.13 -m venv server/.venv
server/.venv/bin/pip install -e common -e server
server/.venv/bin/pip install uvloop      # optional: faster event loop
```

Homebrew's `postgis` formula builds for `postgresql@17` and `@18`.
`postgresql@17` is keg-only, so put its tools on your `PATH`:

```bash
echo 'export PATH="$(brew --prefix postgresql@17)/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

(The official `postgis/postgis` Docker images are amd64-only, so on Apple
silicon a native install is simpler than Docker.)

## 3. Database

```bash
sudo brew services start postgresql@17   # as a LaunchDaemon: starts at boot
createdb uapfinder                        # owned by your macOS user (a superuser)
```

The first migration runs `CREATE EXTENSION postgis`, which needs a
superuser, so the server connects as your own macOS user over the local
socket.

## 4. Configure the server

```bash
cp server/.env.example server/.env
server/.venv/bin/skynet-server keygen          # paste as SKYNET_MASTER_KEY
openssl rand -hex 32                           # paste as SKYNET_ADMIN_TOKEN
```

Edit `server/.env`:

```bash
SKYNET_MASTER_KEY=…                     # never share; node keys derive from it
SKYNET_ADMIN_TOKEN=…
SKYNET_DATABASE_URL=postgresql://localhost/uapfinder   # Homebrew trusts local connections
# Where nodes reach the server (clip uploads): the Mac's tailnet name.
SKYNET_PUBLIC_URL=http://mac-studio:8000
# The public site/map origins allowed to call the API from browsers.
SKYNET_CORS_ORIGINS=https://your-project.vercel.app,https://your-domain.example
SKYNET_STORAGE_DIR=/Users/you/uapfinder-data/clips
```

Replace `mac-studio` with the Mac's machine name in the Tailscale admin
console (MagicDNS). Leave `SKYNET_TRUST_PROXY` unset: uvicorn already takes
the visitor's address from `X-Forwarded-For` when the connection comes from
`127.0.0.1`, which is where Tailscale's Funnel proxy connects from, and
tailscaled overwrites that header with the real client address
([source](https://github.com/tailscale/tailscale/blob/main/ipn/ipnlocal/serve.go)).

Then create the tables and try it in the foreground:

```bash
server/.venv/bin/skynet-server --env-file server/.env migrate
server/.venv/bin/skynet-server --env-file server/.env serve
curl -s http://127.0.0.1:8000/api/v1/health
```

macOS may ask whether Python may accept incoming connections: allow it, or
nodes cannot reach UDP 9999 and HTTP 8000.

## 5. Run it at boot (launchd)

[`org.uapfinder.server.plist`](org.uapfinder.server.plist) is a
LaunchDaemon template. Fill in your user name and paths, then:

```bash
sed -e "s|__USER__|$(whoami)|g" -e "s|__REPO__|$HOME/uapfinder|g" \
    deploy/mac-studio/org.uapfinder.server.plist > /tmp/org.uapfinder.server.plist
mkdir -p ~/uapfinder/data/logs
sudo install -o root -g wheel -m 644 /tmp/org.uapfinder.server.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/org.uapfinder.server.plist

# later:
sudo launchctl kickstart -k system/org.uapfinder.server   # restart after an update
sudo launchctl bootout system/org.uapfinder.server        # stop and unload
tail -f ~/uapfinder/data/logs/server.log
```

launchd restarts the server if it exits (`KeepAlive`), at most every 10 s.

## 6. Tailscale

1. Install the Tailscale app on the Mac and sign in. Enable **MagicDNS** in
   the admin console, and note the Mac's name (e.g. `mac-studio`).
2. Paste the access policy from [`tailscale-policy.hujson`](tailscale-policy.hujson)
   into **Access controls**, replacing the Mac's address in `hosts`. It lets
   edge nodes (tagged `tag:skynet-node`) reach only the server's UDP 9999
   and TCP 8000, keeps your own devices' access, and allows Funnel on your
   machines. Edge nodes cannot reach anything else on your network.
3. Create an **auth key** (Settings → Keys) that is reusable, pre-approved
   and tagged `tag:skynet-node`, and use it as `TS_AUTHKEY` when running
   `edge/deploy/jetson/setup.sh` on each Jetson.
4. Publish the API through Funnel (the CLI is in the Standalone app's
   settings, or `/Applications/Tailscale.app/Contents/MacOS/Tailscale`):

   ```bash
   tailscale funnel --bg 8000
   tailscale funnel status     # shows https://mac-studio.<tailnet>.ts.net
   ```

   Funnel only serves HTTPS on ports 443, 8443 and 10000, has
   bandwidth limits that Tailscale does not publish, and may not carry
   WebSockets (the map falls back to polling automatically). Admin API calls
   are refused when they arrive through Funnel; make them from the tailnet,
   e.g. `http://mac-studio:8000`.

## 7. Register nodes

From any of your own tailnet devices:

```bash
curl -s -X POST http://mac-studio:8000/api/v1/nodes \
  -H "Authorization: Bearer $SKYNET_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"label": "Roof, Surprise AZ"}'
# → {"node_id": "…", "node_key": "…", …}
```

Use the returned `node_id` and `node_key` with the Jetson setup script:

```bash
sudo SKYNET_NODE_ID=… SKYNET_NODE_KEY=… SKYNET_SERVER_HOST=mac-studio \
     TS_AUTHKEY=tskey-auth-… ./edge/deploy/jetson/setup.sh
```

The node then sends UDP to `mac-studio:9999` and uploads clips to
`http://mac-studio:8000`, all inside the tailnet.

## 8. Point the website and map at the Mac

In the Vercel project (imported from this GitHub repository as described in
[docs/DEPLOY_VERCEL.md](../../docs/DEPLOY_VERCEL.md); `vercel.json` builds
the site at `/` and the map at `/map/`), add the environment variable

```
UAPF_API_BASE=https://mac-studio.<tailnet>.ts.net
```

and redeploy. The site's live stats and sign-up form and the map's live
feed then call the Mac, and the map stops showing demo data. Remember to add
the Vercel domain to `SKYNET_CORS_ORIGINS` in `server/.env`.

## 9. Check capacity on this machine

```bash
server/.venv/bin/python server/bench/bench_inprocess.py
```

It reports how many messages per second one server process matches on this
Mac. The blueprint's target is 10,000 msg/s; a 2.1 GHz cloud core manages
about 7,900, and Apple silicon cores are typically considerably faster.
`server/bench/bench_ingest.py` measures the full UDP path.

## Backups

Back up the database and clips regularly, for example nightly:

```bash
pg_dump -Fc uapfinder > ~/uapfinder-backups/uapfinder-$(date +%F).dump
```

Volunteer sign-ups are personal data: keep backups private and delete them
on schedule (see the site's privacy notice).
