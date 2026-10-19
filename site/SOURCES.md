# Sources for the website's facts and prices

Researched on 2026-10-19. Prices are approximate, in US dollars unless noted,
and were volatile during 2026; re-check them before relying on the parts list
(`src/pages/host.astro`, and `PRICES_AS_OF` in `src/config.ts`). Many retailer
pages could only be seen through search-engine snapshots, so treat individual
figures as indicative.

## In-repository sources (system behaviour and numbers)

| Topic | Source |
|-------|--------|
| Error model, error-budget table, 0.047°/pixel, IMU ≈ 1°, refraction, GPS 2–5 m, Mach 6 ≈ 100 m in 50 ms | `docs/MATH.md` |
| Telemetry/heartbeat fields, 10 Hz reporting, 10 s heartbeats, signing, stale/replay rejection, UDP vs HTTPS | `docs/PROTOCOL.md` |
| Stats, volunteer sign-up rules, 1 km public rounding, coverage map, classification thresholds | `docs/API.md` |
| Gate (parallax ≥ 1°, 150 m–400 km range, −0.5–150 km altitude, 3σ + 30 m) | `common/skynet_common/triangulation.py` |
| 80-mile radius, 50 ms window, ±5 s clip window | `server/skynet_server/config.py` |
| Kinematic fits, 2σ lower bounds, ballistic rule | `server/skynet_server/tracks.py` |
| Filters (sidereal rate, wingbeat 2–25 Hz, ADS-B, satellites/meteors off by default) | `edge/skynet_edge/filters.py` |
| 1080p60 / 90° defaults, 10 s RAM buffer, 3° sky mask, 14-day/50 GB local clip retention, poll intervals, no NVENC on Orin Nano, 187 MB/s raw clips | `edge/skynet_edge/config.py`, `clips.py`, `ringbuffer.py`, `capture.py` |
| Message sizes (≈ 520 B telemetry, ≈ 546 B heartbeat, signed, with a UUID node id) | measured with `skynet_common.protocol.encode_signed` |

## Hardware and prices

- **Jetson Orin Nano Super Developer Kit, $249 → $399 (July 2026), often out of stock**
  - CNX Software, 2026-07-22: https://www.cnx-software.com/2026/07/22/nvidia-increases-the-price-of-jetson-modules-and-devkits-by-up-to-101/
  - Hardware Busters: https://hwbusters.com/news/nvidia-jetson-prices-jump-up-to-101-the-249-orin-nano-super-is-now-399/
  - NVIDIA Marketplace listing ($399, out of stock): https://marketplace.nvidia.com/en-us/enterprise/robotics-edge/jetson-orin-nano-super-developer-kit/
  - Price history (US $399 in October 2026): https://pricehistory.app/p/nvidia-jetson-orin-nano-super-developer-kit-JKYHZuce
- **Specs, Super mode via JetPack 6.2 on the original kit, 25 W cap**
  - NVIDIA blog: https://developer.nvidia.com/blog/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules
  - CNX launch article: https://cnx-software.com/2024/12/18/249-nvidia-jetson-orin-nano-super-developer-kit-targets-generative-ai-applications-at-the-edge/
  - NVIDIA forum (25 W maximum, tegrastats): https://forums.developer.nvidia.com/t/reducing-idle-power-on-orin-nano-super-dev-kit/358482
- **Carrier board: 2 × 22-pin MIPI CSI-2, M.2 Key M slots, 19 V adapter included**
  - Carrier board specification SP-11324-001 (mirror): https://uelectronics.com/wp-content/uploads/2023/05/Jetson-Orin-Nano-DevKit-Carrier-Board-Specification.pdf
  - RidgeRun wiki (19 V adapter in the box): https://developer.ridgerun.com/wiki/index.php/NVIDIA_Jetson_Orin_Nano/Introduction/Carrier_Boards/NVIDIA_developer_kit
- **Camera: Arducam IMX477 HQ for Jetson (B0249), ≈ $60–76, 22-pin cable included, works on Orin Nano**
  - Uctronics/Arducam listing ($59.99, was $75.99): https://www.uctronics.com/arducam-high-quality-camera-for-jetson-nano-12-3mp-1-2-3-inch-imx477-hq-camera-module-with-6mm-cs-mount-lens.html
  - RobotShop ($63.14): https://www.robotshop.com/products/arducam-123mp-imx477-hq-camera-module-w-cs-mount-jetson-nano-xavier-nx
  - Arducam wiki: https://docs.arducam.com/Nvidia-Jetson-Camera/Native-Camera/imx477/
  - IMX477 sensor modes incl. 1920 × 1080 at 60 fps: https://forum.arducam.com/t/gst-argus-available-sensor-modes-for-only-one-hq-camera-not-the-camarray-board/1451
  - IMX477 on Orin Nano / JetPack 6 via jetson-io (community reports): https://forums.developer.nvidia.com/t/raspberry-pi-hq-camera-imx477-not-detected-on-jetson-orin-nano-with-jetpack-6-1-help-me/343813
  - 15-pin to 22-pin cabling: https://developer.ridgerun.com/wiki/index.php/NVIDIA_Jetson_Orin_Nano/Camera_Sensors_Support/IMX219
  - jetson-io camera configuration required from JetPack 6.2: https://jetsonhacks.com/2025/03/04/jetson-orin-nano-super-with-the-raspberry-pi-v2-and-v3-cameras/
- **Lens: Arducam 90° M12 lens M23356H09 (3.56 mm, 1/2.3"), ≈ $19 (RobotShop), £17–22 (The Pi Hut)**
  - https://robotshop.com/products/arducam-90-wide-angle-1-23-m12-lens-w-lens-adapter-for-raspberry-pi
  - https://thepihut.com/products/m12-lens-90-degree-wide-angle-with-raspberry-pi-hq-camera-adapter
- **GPS with PPS**
  - Adafruit Ultimate GPS Breakout v3 (PA1616S, PPS output), $29.95: https://www.adafruit.com/product/746
  - SparkFun GPS Breakout NEO-M9N (U.FL), $70.95: https://www.sparkfun.com/sparkfun-gps-breakout-neo-m9n-u-fl-qwiic.html
  - SparkFun hookup guide (broken-out PPS pin): https://learn.sparkfun.com/tutorials/sparkfun-gps-neo-m9n-hookup-guide/hardware-overview
  - Adafruit active GPS antenna 28 dB SMA (product 960), $21.50: https://www.adafruit.com/product/960
- **Orientation sensor: Adafruit BNO055 (product 4646), $29.95:** https://www.adafruit.com/product/4646
- **NVMe storage (prices up sharply in 2026; 1 TB ≈ $120–180 in mid-October 2026; 256–512 GB range estimated at $60–150)**
  - https://dropreference.com/en/blog/news/ssd-price-increase-2026-nand-flash-crisis
  - https://www.harddiskprices.com/articles/ssd-prices-going-up
  - https://datacenterdisk.com/price-forecast/ssd
  - 512 GB example listing ($130.92, Aug 2026): https://camelcamelcamel.com/product/B0CXTQTD22
- **ADS-B receiver**
  - RTL-SDR Blog V4L, $37.95 (V4 discontinued): https://www.rtl-sdr.com/product/rtl-sdr-blog-v4l-lite-r828s-rtl2832u-1ppm-tcxo-sma-software-defined-radio-dongle-only/
  - FlightAware Pro Stick Plus and a 1090 MHz antenna (£24.90) at The Pi Hut: https://thepihut.com/products/flightaware-pro-stick-plus-usb-sdr-ads-b-receiver
- **Enclosure (estimate):** all-sky camera parts list (100 mm acrylic dome ≈ €16, 2.8 W heater ring ≈ €18.50 + relay ≈ €6.80): https://allskykamera.space/kamerabau.php?lang=en

## Power cost

- US EIA Short-Term Energy Outlook, 2026 average residential price ≈ 18.3 ¢/kWh: https://www.eia.gov/outlooks/steo/tables/pdf/SF02.pdf
- April 2026 national average 18.83 ¢/kWh (EIA data, via Newsweek): https://www.newsweek.com/map-electricity-prices-each-state-trump-deadline-passes-12222810

## Science

- Meteoroid entry speeds ≈ 11.2–72.5 km/s (review): https://arxiv.org/pdf/1805.07842
- Most meteor ablation at ≈ 80–125 km: https://arxiv.org/pdf/astro-ph/0505288

## Legal and privacy

- CJEU C-212/13 *Ryneš* (11 Dec 2014): a home camera that also covers public space is not "purely household" processing.
  - https://www.thomashelbing.com/en/wissen/dsgvo-hub/rechtsprechung/1.4.20-eugh-rynes
  - https://fra.europa.eu/en/node/17582
  - https://www.mccannfitzgerald.com/knowledge/privacy/beyond-safe-harbor-other-important-data-protection-decisions-of-2015

## Framework and tooling

- Astro 7 upgrade guide (Rust compiler, `compressHTML: 'jsx'` default, Vite 8): https://raw.githubusercontent.com/withastro/docs/main/src/content/docs/en/guides/upgrade-to/v7.mdx
- Package versions and Node engine requirements: https://registry.npmjs.org/ (astro 7.4.1 requires Node ≥ 22.22.0; `@astrojs/check` 0.9.11 supports TypeScript 5–6)
- Vercel system environment variables (`VERCEL_PROJECT_PRODUCTION_URL`, no scheme): https://vercel.com/docs/environment-variables/system-environment-variables
- ISO 3166-1 alpha-2 codes: https://github.com/lukes/ISO-3166-Countries-with-Regional-Codes (`all/all.csv`)
