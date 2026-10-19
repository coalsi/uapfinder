# SKY-NET edge node (`skynet-edge`)

Software for one UAP Finder camera node: it watches the sky at 1080p60,
finds and tracks moving points, throws away what it can explain (stars,
clouds, birds, aircraft with ADS-B), and streams the line of sight to every
unexplained object to the server, which triangulates it against other
nodes. When the server confirms a stereo match it asks the node for the
video around the event, which the node keeps in a RAM ring buffer.

> **Hardware status.** Everything here was developed and tested on an x86
> container with synthetic video. Nothing has run on a Jetson, a CSI
> camera, a GPS/PPS receiver, a BNO055 or CUDA yet. Code for those parts
> follows the vendors' documentation and is marked *not hardware-tested*;
> validate it with the [procedure below](#validation-on-hardware) before
> trusting a node's output.

```
camera ──► capture thread ──► ring buffer (RAM, last N s, I420) ──► clip export ─► MP4 ─► upload
             (timestamp)            │                                    ▲
                                    ▼                                    │ clip_request (signed)
           detection ─► tracking ─► kinematics + filters ─► telemetry ─► UDP / HTTPS ─► server
           (background,  (Kalman,     (anomaly score;      (10 Hz per
            threshold,    Hungarian,   slow/cloud/bird/     reportable
            blobs)        LK bridge)   aircraft/satellite)  track)
   GPS (gpsd/NMEA/static) ─┘   attitude (static/BNO055) ─┘   ADS-B (readsb / ADSBx-v2 API) ─┘
```

## Hardware

| Part | Recommendation | Notes |
|------|----------------|-------|
| Computer | **Jetson Orin Nano 8 GB Developer Kit** (the "Super" kit, or the original kit updated to JetPack 6.2) | 8 GB shared CPU/GPU memory. The ring buffer default (14 s of 1080p60) uses 2.6 GB. |
| Storage | NVMe SSD, 256 GB or more | Clips are exported raw at ~190 MB/s before encoding; a microSD card cannot keep up. |
| Camera | CSI: **IMX477** (Raspberry Pi HQ camera / Arducam), which has a 1920x1080 @ 60 fps mode on Jetson. IMX219 tops out at 1080p30 (set `camera.fps: 30`). USB UVC cameras work through `camera.source: v4l2`. | Lens around 70-100° horizontal FOV. Mount behind a clear dome with a dew heater. |
| GPS | u-blox M8/M9/M10 module with a **PPS output** on a UART | PPS gives microsecond timestamps; without it timing is NTP-grade (milliseconds), which costs tens of metres on fast targets. |
| IMU (optional) | BNO055 breakout on I2C (header pins 3/5 = `/dev/i2c-7` on the devkit) | Only for temporary/mobile mounts; see [Pointing](#pointing-from-a-star-field). |
| Power | The devkit's supply, ideally behind a small UPS | Check the supply rating before using the 25 W or MAXN SUPER power modes. |

## Install

* **Fresh devkit:** follow [`deploy/image/README.md`](deploy/image/README.md)
  (path A: NVIDIA's SD-card image written with Etcher, after the one-time
  firmware update; path B: a custom image from the L4T BSP).
* **Provisioning** (both paths end here): [`deploy/jetson/setup.sh`](deploy/jetson/setup.sh)
  installs gpsd, chrony, pps-tools, ffmpeg and GStreamer tools, creates the
  `skynet` system user, a venv with `--system-site-packages` (so the
  JetPack OpenCV is used), the package, the gpsd/udev/chrony configuration
  with PPS detection, the systemd unit and, optionally, joins Tailscale and
  sets the power mode:
  ```bash
  sudo SKYNET_NODE_ID=node-a SKYNET_NODE_KEY=<64 hex> SKYNET_SERVER_HOST=mac-studio \
       TS_AUTHKEY=tskey-auth-... SKYNET_NVPMODEL=<mode id> ./edge/deploy/jetson/setup.sh
  ```
  The node id and key come from the server (`POST /api/v1/nodes`, docs/API.md).
  With Tailscale, create a tagged auth key (or list `tag:skynet-node` under
  `tagOwners` in the tailnet policy) so nodes join as tagged devices.
* **Development machine:**
  ```bash
  python3 -m venv edge/.venv
  edge/.venv/bin/pip install -e common -e 'edge[test]'
  edge/.venv/bin/pytest edge/tests
  ```

### Deploying against a Mac Studio over Tailscale

The server runs on the Mac; nodes send UDP to its tailnet address:

```yaml
server:
  transport: udp
  udp_host: mac-studio              # MagicDNS name or 100.x.y.z; re-resolved every minute
  udp_port: 9999
  https_base_url: http://mac-studio:8000
```

`https_base_url` matters even with UDP: a server started without
`SKYNET_PUBLIC_URL` puts its own loopback address in `clip_request.upload_url`
(`http://127.0.0.1:8000/...`). The node replaces loopback hosts with
`https_base_url` before uploading (`server.rewrite_loopback_urls`). Setting
`SKYNET_PUBLIC_URL=http://mac-studio:8000` on the server is cleaner.

## Configuration

[`config/example.yaml`](config/example.yaml) documents every section. The
loader rejects unknown keys and out-of-range values at start-up. The parts
you must set per node:

* `node.node_id`, `node.key_file`
* `server.*` (above)
* `camera.*`: source, resolution/fps, `capture_latency_ms`, and either
  calibrated `intrinsics` (fx, fy, cx, cy, OpenCV distortion) or `fov_h_deg`
* `attitude.*`: the optical axis (azimuth from **true** north, elevation,
  roll with right side down positive), from a star-field solution
* `gps.static`: the surveyed position, height above the **WGS84 ellipsoid**
  (not MSL; GPS altitude + geoid separation). Used as the fallback until
  the first fix.
* `detection.sky_mask`: minimum elevation and/or a pixel polygon
* `adsb.*`: a local readsb/dump1090 `aircraft.json`, or an ADSBx-v2-style
  API (`https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}`, ADS-B
  Exchange with `api_key`)

Reporting policy (`anomaly.*`): by default **every confirmed track that no
filter explains is reported** with its `anomaly_score`. One camera measures
only angles, so it cannot tell a fast distant object from a slow near one;
the server's triangulation decides. `report_unidentified: false` reports
only tracks whose peak score reaches `min_anomaly_score`.

## Commands

| Command | What it does |
|---------|--------------|
| `skynet-edge run --config /etc/skynet/edge.yaml` | The node (what the systemd unit runs). |
| `skynet-edge bench` | Synthetic 1920x1080@60 through the full pipeline; prints sustained FPS, per-stage ms, peak memory and PASS/FAIL against ≥60 FPS and <7 GB. `--unpaced` measures maximum throughput, `--config` uses a node's detection settings. Exit code 2 on FAIL. |
| `skynet-edge replay video.mp4 --config cfg.yaml [--out tel.ndjson] [--send]` | Runs a recorded video through the pipeline and prints what would be reported (or sends it). |
| `skynet-edge make-synthetic sky.mp4 [--config cfg.yaml]` | Renders the synthetic test sky (a turning target, a cloud, a bird, an aircraft) with ground truth, a matching `aircraft.json` and a replay config. |

`journalctl -u skynet-edge -f` shows a status line every minute; the
heartbeat (every 10 s) carries fps, per-stage ms, memory, temperatures,
GPS/clock state and filter counts to the server.

## How it works (and why)

* **Timestamps** are `time.time_ns() - capture_latency_ns` at frame
  arrival, taken on a dedicated capture thread. They are only as good as
  the clock (chrony locked to the GPS PPS) and the latency calibration.
* **Ring buffer:** preallocated, prefaulted I420 frames (`ring_buffer.seconds`
  plus `hold_seconds` of spare slots), size checked against
  `memory_budget_mb` at start-up. A trigger *protects* a time window; frames
  in it are held in spare slots instead of being overwritten (the camera
  never waits), a background thread copies them to Y4M, and ffmpeg/libx264
  encodes at idle priority. The **Orin Nano has no hardware video encoder**
  (NVIDIA: "Software Encode in Orin Nano"), so encoding is CPU-only,
  deferred and limited to event clips.
* **Detection:** rolling-average background (2 s time constant: absorbs
  the sky gradient, haze and the 0.0042°/s drift of stars), `|frame - background|`,
  per-tile robust noise (median absolute difference), max-pool decimation
  (keeps 1-2 px targets at full contrast), hysteresis threshold, closing,
  connected components, intensity-weighted sub-pixel centroids. Boxes of
  young or moving tracks update slowly so slow targets do not fade into
  the background; stationary ghosts left by a background reset fade
  normally. Dark silhouettes (birds, aircraft by day) are detected too.
* **Backends:** `cpu` (OpenCV) is the tested path. JetPack 6's OpenCV is
  built **without CUDA** (NVIDIA forum), so `cv2.cuda` would need a custom
  build; the `cuda` backend runs the per-pixel stages with **CuPy**
  (`pip install cupy-cuda12x`, aarch64 wheels exist), with only small masks
  and ROIs copied back. It is exercised in tests with NumPy standing in for
  CuPy but is **not hardware-tested**; `auto` falls back to `cpu`.
* **Tracking:** constant-velocity Kalman filter per track, Mahalanobis gate,
  Hungarian assignment per gate-connected component, tentative ->
  confirmed -> lost. Pyramidal Lucas-Kanade seeded with the Kalman
  prediction bridges missed detections; coarse Farneback flow gives the
  cloud motion field, computed lazily only when a diffuse candidate needs
  it. Centroids go through `cv2.undistortPoints` and
  `skynet_common.camera.pixel_to_azel`.
* **Filters** (each returns a label and a reason; labelled tracks are not
  reported): `slow` (< 0.03°/s), `cloud` (diffuse, low contrast, slow or
  moving with the flow field), `aircraft` (ADS-B position dead-reckoned to
  each sample time, `geodesy.look_angles`, median separation within 1°, 2°
  for barometric altitude), `bird` (large and either flapping at 2-25 Hz,
  or both expanding and defocused), optional `satellite`/`meteor`. Labels
  are held for a few seconds (aircraft 30 s) so they do not flicker; `slow`
  is not held, so a hovering object that darts away is reported at once.
* **Anomaly score:** robust (Huber) quadratic fit of the last 0.5 s in a
  gnomonic projection, where great-circle motion is a straight line;
  significant (> 3 σ) angular acceleration and turn rate map to 0..1.
* **Uplink:** signed messages from `skynet_common.protocol`; UDP
  (non-blocking, same socket receives verified `clip_request`s) or HTTPS
  NDJSON batches every 100 ms with command polling; monotonic `seq`
  starting at Unix ms; heartbeats every 10 s. Clip uploads follow API.md's
  upload-target / PUT / complete sequence with retries.

## Calibration

### Camera intrinsics (checkerboard)

Print a 9x6 inner-corner checkerboard (25-30 mm squares, flat on rigid
board). Capture 20-40 full-resolution frames covering the whole field,
including the corners, at various tilts (e.g.
`gst-launch-1.0 nvarguscamerasrc num-buffers=1 ! 'video/x-raw(memory:NVMM),width=1920,height=1080' ! nvjpegenc ! filesink location=cal_01.jpg`),
then:

```python
import glob, cv2, numpy as np
pattern, square = (9, 6), 0.025
obj = np.zeros((pattern[0] * pattern[1], 3), np.float32)
obj[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * square
objs, imgs = [], []
for f in sorted(glob.glob("cal_*.jpg")):
    g = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
    ok, c = cv2.findChessboardCorners(g, pattern)
    if ok:
        objs.append(obj)
        imgs.append(cv2.cornerSubPix(g, c, (11, 11), (-1, -1), (3, 30, 1e-3)))
rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, g.shape[::-1], None, None)
print(rms, K, dist.ravel())   # aim for rms < 0.3 px
```

Put `fx=K[0,0]`, `fy=K[1,1]`, `cx=K[0,2]`, `cy=K[1,2]` and the five `dist`
values under `camera.intrinsics`. Pixel centres are integers (OpenCV
convention) throughout.

### Capture latency

Film an LED driven by the GPS PPS output (or a GPS-synced display) and
compare the frame timestamp of the first lit frame with the true second,
over a few hundred pulses; the average difference (minus half an exposure)
is `camera.capture_latency_ms`. An uncalibrated latency shifts every
direction along the track by angular speed x latency.

### Pointing from a star field

Pointing error dominates the triangulation error budget (docs/MATH.md): a
1° yaw error is 50 km x tan(1°) ≈ **870 m** of lateral error at 50 km
range, while centroiding is good to ~0.01-0.05°. A consumer IMU's
magnetometer heading is rarely better than 1-2°, so fixed nodes should use
`attitude.source: static` with a star-field solution:

1. On a clear night take a long exposure (lower the frame rate and set
   `exposuretimerange` on `nvarguscamerasrc`), record the UTC time.
2. Plate-solve it with astrometry.net (`solve-field frame.png`).
3. With astropy, convert the RA/Dec of the image centre (`cx, cy`) and of a
   point 200 px to the right of it to azimuth/elevation (`AltAz` at the
   node's position and the frame time).
4. `yaw_deg`/`pitch_deg` = az/el of the centre. `roll_deg` is the value for
   which `CameraModel.pixel_to_azel(cx + 200, cy, Attitude(yaw, pitch, roll))`
   matches the second point (a 1-D search over roll).

Re-check after storms or maintenance; a BNO055 (if fitted) is useful to
detect that a mount has moved even if it is not accurate enough to point.

### BNO055 (if used)

NDOF fusion mode, Euler angles from registers 0x1A-0x1F (1/16° per LSB).
Heading is magnetic: set `magnetic_declination_deg` (east positive) for the
site. The mount offsets are first-order corrections for an IMU mounted
roughly level; calibrate them against a star-field solution. The calibration
status (system/gyro/accel/mag, 0-3) is in the heartbeat as `imu_calib`.

### Time (chrony + PPS)

`setup.sh` configures gpsd to feed NMEA time to chrony (shared memory 0) and
chrony to lock the PPS pulse to it. On Jetson kernels `/dev/pps0` is often
the `ktimer` test source; the GPS pulse needs a `pps-gpio` device-tree
overlay for the header pin it is wired to (build it with `dtc -@` and enable
it with jetson-io or an `OVERLAYS` line in `/boot/extlinux/extlinux.conf`),
which then appears as `/dev/pps1`. Re-run `setup.sh` after adding it. Check
with `sudo ppstest /dev/pps1` and `chronyc sources -v` (`#* PPS`, offsets in
microseconds). *Not hardware-tested.*

## Validation on hardware

1. **Platform:** `cat /etc/nv_tegra_release` (R36), `sudo nvpmodel -q`, a
   heatsink fan that spins.
2. **Camera:** `gst-launch-1.0 nvarguscamerasrc num-buffers=600 ! 'video/x-raw(memory:NVMM),width=1920,height=1080,framerate=60/1' ! fakesink -v`
   (sensor mode list in the log), then check the node's `fps` in the status line.
3. **OpenCV:** `/opt/skynet/venv/bin/python -c "import cv2; print(cv2.getBuildInformation())" | grep -i gstreamer`.
   Without GStreamer the node captures through `gst-launch-1.0` automatically.
4. **Time and position:** `chronyc tracking`, `gpspipe -w -n 10 | grep TPV`
   (`mode` 3, `altHAE`).
5. **Benchmark** in the intended power mode, with `sudo tegrastats --interval 1000`
   or `jtop` (`sudo pip3 install jetson-stats`) alongside:
   ```bash
   sudo -u skynet /opt/skynet/venv/bin/skynet-edge bench --config /etc/skynet/edge.yaml --json /tmp/bench.json
   ```
   Requirement: ≥ 60 FPS sustained at 1080p with no drops, < 7 GB. Also try
   `--backend cuda` if CuPy is installed (`SKYNET_CUDA=1`) and
   `--clips-dir /var/lib/skynet/clips` to include disk I/O. If it fails on
   the CPU backend, raise `detection.decimation` to 4 (point targets are
   kept by max-pooling), `detection.background_update_every` to 3-4, or
   lower `tracking.analysis_hz`.
6. **End to end:** `skynet-edge make-synthetic sky.mp4` then
   `skynet-edge replay sky.mp4 --config sky.yaml --send` (after putting the
   node's key and server into `sky.yaml`): the server should show one track.
7. **Real sky:** compare reported tracks with ADS-B for a few hours of
   daytime traffic (they should be labelled `aircraft`, not reported) and
   with a known satellite pass at night (`filters.satellite.enabled: true`).

### Benchmark numbers (x86 container, NOT a Jetson)

`skynet-edge bench --seconds 20` on a 4-vCPU Intel Xeon @ 2.1 GHz container,
CPU backend:

| Mode | Sustained FPS | Dropped | Mean / p95 pipeline ms | Peak RSS |
|------|--------------:|--------:|-----------------------:|---------:|
| paced 1920x1080@60 | 60.0 | 0 / 1200 | 7.2 / 11.6 | 2617 MiB |
| unpaced (max throughput) | 146 | 0 | 6.7 / 10.6 | 2617 MiB |

Per stage (paced, mean ms): detection incl. background update 5.1,
tracking 0.6, flow 0.14, filters + kinematics 1.4; ring-buffer copy 0.6 on
the capture thread. The Orin Nano's six Cortex-A78AE cores are slower than
this machine; expect several times these timings on the CPU backend and
measure before deploying.

## What is not hardware-tested

CSI capture through `nvarguscamerasrc` (OpenCV GStreamer and `gst-launch`
subprocess paths; the latter is tested with ffmpeg), V4L2 capture, the CuPy
backend on a GPU, gpsd with a real receiver (tested against a fake gpsd),
NMEA from a real serial port, PPS and chrony discipline, the BNO055 driver
(tested against a fake SMBus), thermal zones and memory on Jetson, sd_notify
under systemd, `setup.sh`, `customize_rootfs.sh` and flashing, Tailscale
join, nvpmodel modes, and the systemd hardening options with real devices.

## Research notes and sources

* Orin Nano has no NVENC; software encoding with libx264 is NVIDIA's
  documented path: https://docs.nvidia.com/jetson/archives/r36.3/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html
* JetPack OpenCV is built without CUDA: https://forums.developer.nvidia.com/t/opencv-4-8-0-with-cuda-no/329890 ,
  https://forums.developer.nvidia.com/t/gpu-opencv/293782 . Reports on its
  GStreamer support conflict (https://forums.developer.nvidia.com/t/uninstalling-opencv-4-8-0-from-agx-orin-developer-kit-64gb-l4t-36-3/319267),
  hence the `gst-launch-1.0` fallback.
* OpenCV returns an `I420` appsink buffer as one (1.5 H, W) plane
  (`cap_gstreamer.cpp`, 4.8.0): https://github.com/opencv/opencv/blob/4.8.0/modules/videoio/src/cap_gstreamer.cpp
* Argus clients talk to nvargus-daemon over `/tmp/argus_socket` (so no
  `PrivateTmp=` in the unit): https://forums.developer.nvidia.com/t/running-argus-sample-applications-in-a-container/145925
* systemd `ProtectClock=` implies `DeviceAllow=char-rtc r` (a closed device
  policy that would block the camera, I2C and serial devices):
  https://github.com/systemd/systemd/blob/v249/man/systemd.exec.xml
* CuPy publishes aarch64 `cupy-cuda12x` wheels: https://docs.cupy.dev/en/stable/install.html
* IMX477 has a 1920x1080 @ 60 fps mode on Orin: https://developer.ridgerun.com/wiki/index.php/NVIDIA_Jetson_Orin/Video_Capture_and_Display/Cameras/IMX477_J20
* JetPack 6.2 Super mode (`nvpmodel -m 2` = MAXN SUPER on Orin Nano, needs
  the `-super` flash configuration): https://developer.nvidia.com/blog/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules ,
  https://forums.developer.nvidia.com/t/jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules/320343
* Header UART is `/dev/ttyTHS1` on JetPack 6: https://forums.developer.nvidia.com/t/how-to-access-uart1-on-expansion-pins-8-10/328934
* PPS on Orin Nano (pps-gpio overlay; `pps0` is the ktimer): https://forums.developer.nvidia.com/t/guidance-on-enabling-gps-pps-input-on-jetson-orin-nano-developer-kit/376687
* gpsd TPV fields (`altHAE`, `altMSL`, `geoidSep`, `mode`) and the time-service
  howto (SHM 0/1, `-n`): https://gpsd.io/gpsd_json.html , https://gpsd.io/gpsd-time-service-howto.html
* chrony `refclock PPS ... lock NMEA` and `SHM`: https://chrony-project.org/doc/4.2/chrony.conf.html
* readsb `aircraft.json` fields (`alt_geom` is WGS84 ellipsoid height, `seen_pos`):
  https://github.com/wiedehopf/readsb/blob/dev/README-json.md ; ADSBx-v2-compatible
  APIs: https://github.com/adsbfi/opendata
* BNO055 register map as used by Adafruit's driver: https://github.com/adafruit/Adafruit_CircuitPython_BNO055/blob/main/adafruit_bno055.py
* Tailscale installer and `tailscale up --auth-key/--advertise-tags/--hostname`:
  https://github.com/tailscale/tailscale/blob/main/scripts/installer.sh ,
  https://tailscale.com/kb/1241/tailscale-up
