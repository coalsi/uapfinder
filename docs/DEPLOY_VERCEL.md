# Deploy the website and live map on Vercel

One Vercel project, built from the **repository root**, serves the public
website at `/` and the live 3D map at `/map/`. Everything is configured by
[`vercel.json`](../vercel.json): the build runs
[`scripts/build-web.sh`](../scripts/build-web.sh), which builds `site/` and
`dashboard/` and combines them into `site/dist`.

By default the project is **static only**. The real-time server runs
elsewhere (the Mac Studio, see [deploy/mac-studio](../deploy/mac-studio/README.md))
and the site and map call it by its public address. Until that address is set,
the map shows a clearly labelled demo network, and the site's live stats show
"network launching".

## Import the repository (one project)

1. In Vercel, choose **Add New… → Project**, and under **Import Git
   Repository** pick `coalsi/uapfinder`. If it isn't listed, use **Adjust
   GitHub App Permissions** to give Vercel access to the repository.
2. **If Vercel says it found multiple projects** (it detects the website in
   `site/` and the map in `dashboard/`): import **one** project, with the
   **repository root** as its Root Directory. Don't create separate projects
   for `site` and `dashboard`; the root build already includes both.
3. On **Configure Project**:
   * **Root Directory**: `./` (the repository root). If Vercel pre-filled
     `site` or `dashboard`, click **Edit** and choose the top level.
   * **Application Preset** (also called Framework Preset): **Other**. When
     Vercel detects several apps it pre-selects **Services**, its beta mode
     for bundling separate apps; open the dropdown and pick **Other**
     instead. This repository is one static build.
   * **Build and Output Settings**: leave the overrides **off**. `vercel.json`
     supplies them: install command (a no-op; each app installs its own
     dependencies during the build), build command `bash scripts/build-web.sh`,
     output directory `site/dist`. (If a field is already filled in from a detected
     framework, turn its override off, or enter exactly these values.)
   * **Environment Variables**: none are needed for the first deploy.
4. Click **Deploy**. The build takes a minute or two (two `npm ci` installs
   and two production builds).

Then open the deployment:

| URL | What you should see |
|-----|---------------------|
| `https://<project>.vercel.app/` | The UAP Finder website |
| `https://<project>.vercel.app/download/` | Download links and set-up steps |
| `https://<project>.vercel.app/map/` | The live 3D map with a **DEMO DATA** label |

Every push to the repository's default branch redeploys production; other
branches get preview URLs. The default branch is currently
`claude/stoic-heisenberg-tf9d48`. If you later make `main` the default
branch, change **Settings → Git → Production Branch** to match.

## Settings worth checking

* **Node.js version** (**Settings → Build and Deployment**): 22.x or 24.x.
  The website needs Node.js 22.22 or newer.
* **Domains**: add your own domain under **Settings → Domains** when you have
  one, then set `PUBLIC_SITE_URL` (below) so canonical links and the sitemap
  use it.

## Environment variables

All are read at build time, so **redeploy** after changing them
(**Deployments → ⋯ → Redeploy**).

| Variable | When | Example |
|----------|------|---------|
| `UAPF_API_BASE` | Once the server is reachable. Points the site's live stats and sign-up form, and the map's live feed, at it; the map then stops showing demo data. | `https://mac-studio.tailnet-name.ts.net` |
| `PUBLIC_CONTACT_EMAIL` | Before launch. Shown on the site for questions and data-deletion requests; hidden when unset. | `hello@your-domain.example` |
| `PUBLIC_SITE_URL` | When you add a custom domain. | `https://uapfinder.example` |
| `VITE_DEMO` | Optional. `1` forces the demo map, `0` turns it off. | `1` |
| `VITE_CESIUM_ION_TOKEN` | Optional. A free Cesium ion token adds 3D terrain and better imagery. | |

When `UAPF_API_BASE` points at the Mac Studio, add the Vercel domain (and
your custom domain) to `SKYNET_CORS_ORIGINS` in the server's `server/.env`,
for example `https://uapfinder.vercel.app,https://uapfinder.example`, and
restart the server.

## Optional: run the API on Vercel too

For a small pilot without any server of your own, the API can also run as a
Vercel Function, with Upstash Redis and Neon Postgres from the Vercel
Marketplace. Nodes must then use HTTPS (Vercel cannot receive UDP). Run
[`scripts/enable-vercel-api.sh`](../scripts/enable-vercel-api.sh), commit
what it copies, set the variables listed in
[server/README.md](../server/README.md#deploy-on-vercel), and redeploy. This
mode has not been tested on Vercel itself.

## Troubleshooting

* **"No Output Directory named 'public' found"**: the project isn't using
  `vercel.json`. Check that the Root Directory is the repository root and the
  Output Directory override is off (or set to `site/dist`).
* **The build fails in `npm ci`**: the Node.js version is too old; select
  22.x or 24.x.
* **The map is empty and says it is reconnecting**: `UAPF_API_BASE` points at
  a server that is down or doesn't allow this origin in
  `SKYNET_CORS_ORIGINS`. Unset it (demo data) or fix the server.
