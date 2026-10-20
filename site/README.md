# UAP Finder website

The public website that explains the project and recruits volunteers. A fast,
fully static [Astro 7](https://astro.build) site: TypeScript, system fonts,
hand-drawn inline SVG diagrams and two small vanilla scripts (live network
figures and the volunteer form). No cookies, analytics or third-party requests.

| Path | Page |
|------|------|
| `/` | What UAP Finder is, why stereo, how it works, live network figures |
| `/how-it-works/` | Edge pipeline, uplink, matching, triangulation, error model, tracks and flags, clips, what is published, limitations |
| `/device/` | The node: anatomy, what happens inside, two nodes as a stereo pair (with an optional animation), GPS timing, fixes to tracks, self-calibration from the stars, the setup page (mock-up), specifications and limits |
| `/host/` | Who makes a good host, parts list with approximate prices, power and bandwidth estimates, setup overview, coverage map |
| `/build/` | Build a node step by step: printable parts checklist, firmware check, flashing the image, SSD, camera, GPS wiring, enclosure, placement, first boot, setup page, first night, troubleshooting, command-line alternative |
| `/download/` | The node system image (primary), source archives, server and simulator setup |
| `/get-involved/` | Volunteer interest form (`POST /api/v1/volunteers`) and other ways to help |
| `/faq/` | Frequently asked questions |
| `/privacy/` | Plain-language privacy notice (draft, to be reviewed before launch) |
| `/404.html`, `/sitemap.xml`, `/robots.txt` | Error page and SEO files |

The live 3D map is a separate app (`dashboard/`) served at `/map/`; this site
links to it but never generates anything under `/map/` or `/api/`.

## Develop

Requires Node ≥ 22.22.0 (Astro 7's minimum) and npm.

```sh
npm ci
npm run dev          # http://127.0.0.1:5180
npm run build        # astro check + tsc, then static output in dist/
npm run preview      # serve dist/ on http://127.0.0.1:5181
```

Note: Astro 7's `preview` keeps a per-project lock; `npx astro preview stop`
stops a background preview, and the end-to-end tests pass `--ignore-lock`.

## Tests

```sh
npm test             # Vitest: form validation/serialisation, stats parsing, URLs, countries
npm run build
npm run test:e2e     # Playwright against dist/ with a mocked API; writes screenshots/
npm run screenshot   # only the per-page screenshot tests
```

The end-to-end suite serves the production build on port 5181 (override with
`E2E_PORT`, keep within 5180–5189), intercepts every `/api/v1/*` request and
covers: client-side validation, the exact JSON body (including a geolocation
rounded to 2 decimals), 201 / 422 / 429 / 503 / network-failure states, the
honeypot, the stats widget's live and "network launching" states, SEO tags,
internal links and anchors, keyboard skip link, the mobile menu, reduced
motion and horizontal overflow. Screenshots of every page at 1440 × 900 and
390 × 844 (full page), plus form and widget states, go to `screenshots/`.
Playwright uses its bundled Chromium (`PLAYWRIGHT_BROWSERS_PATH`); set
`CHROMIUM_PATH` to use another binary.

`npm run images` re-renders `public/og.png`, `public/apple-touch-icon.png` and
`public/favicon-32.png` from the artwork in `scripts/render-images.mjs` and
`public/favicon.svg`. The outputs are committed.

## Configuration

All variables are optional and read at **build time** (a change needs a
rebuild). See `.env.example`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUBLIC_SITE_URL` | `https://$VERCEL_PROJECT_PRODUCTION_URL`, else `http://localhost:5180` | Absolute origin for canonical and OpenGraph URLs and the sitemap. Set it for any non-Vercel production build. |
| `PUBLIC_REPO_URL` | `https://github.com/coalsi/uapfinder` | Source links, the edge README (`/tree/main/edge`), docs and issues. |
| `PUBLIC_CONTACT_EMAIL` | unset | Contact address in the footer, FAQ and privacy notice. When unset, all contact-email UI is hidden and deletion requests point to the repository's issue tracker. **Recommended before launch.** |
| `PUBLIC_API_BASE` | `""` (same origin) | Base URL of the server API, without `/api/v1`. If it is another origin, add this site to the server's `SKYNET_CORS_ORIGINS`. |
| `PUBLIC_MAP_URL` | `/map/` | Live 3D map. The coverage layer is linked as `${PUBLIC_MAP_URL}?layer=coverage`. |

## Deployment (Vercel)

- Root directory `site/`, framework preset Astro (or "Other"), build command
  `npm run build`, output directory `dist`, Node 22.x.
- The deployment copies the dashboard build into `dist/map/` after this build.
- If the API is served from the same Vercel project under `/api/`, leave
  `PUBLIC_API_BASE` empty. The site's pages use trailing-slash directory URLs
  (`/host/` → `host/index.html`); Vercel serves `404.html` for unknown paths.
- The live widget calls `GET /api/v1/stats` and `GET /api/v1/volunteers/summary`
  every 30 s while the tab is visible and shows "network launching" if either
  is unreachable. The form shows a clear message for 422, 429 and 503
  (`{"detail": "sign-up unavailable"}` when the server has no database).
- Astro collects anonymous build telemetry by default; set
  `ASTRO_TELEMETRY_DISABLED=1` in the build environment to opt out.

## Layout

```
src/config.ts              build-time config from PUBLIC_* variables
src/lib/volunteer.ts       form validation, serialisation, response handling (pure)
src/lib/stats.ts           stats parsing/formatting and fetch with timeout (pure)
src/lib/countries.ts       ISO 3166-1 alpha-2 codes; names from Intl.DisplayNames
src/scripts/*.ts           browser wiring for the form and the live widget
src/components/*.astro     header, footer, diagrams, error budget, form, widget
src/pages/*.astro          pages; sitemap.xml.ts and robots.txt.ts endpoints
tests/unit, tests/e2e      Vitest and Playwright suites
SOURCES.md                 sources for prices and facts (re-check before launch)
```

## Content rules

Accurate, neutral and scientific: no claims about what UAPs are; numbers come
from `docs/MATH.md`, `docs/API.md`, `docs/PROTOCOL.md` and the code, and
anything estimated says so and gives its basis. Prices carry an "as of" month
(`PRICES_AS_OF` in `src/config.ts`) and are sourced in `SOURCES.md`. Never add
statistics, partners, endorsements, testimonials, team members or contact
details that do not exist.

## Design

Dark "night sky" palette defined as CSS custom properties in
`src/styles/global.css`. All text tokens meet WCAG AA on every surface
(body text ≈ 15–17:1, muted text ≥ 5.8:1, links ≥ 9.8:1); input borders are
≥ 3:1. Layouts are tested down to 390 px, tables stack into labelled rows on
phones, focus is always visible, a skip link is provided and animations only
run when `prefers-reduced-motion` is not set.
