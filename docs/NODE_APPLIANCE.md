# The UAP Finder node as an appliance

Goal: a volunteer with no Linux experience builds a node by **flashing one
microSD card, plugging the parts together, placing it outdoors and turning
it on**. A phone is the only other tool. No terminal, no config files, no
keys to copy.

This document is the shared design for the node software (`edge/`), the
flashable OS image (`edge/deploy/image/`, `.github/workflows/node-image.yml`),
the server's enrollment API (`server/`) and the website's device page and
build guide (`site/`). Where it fixes a name, port or behaviour, all of them
use exactly that.

## What the volunteer does

1. Download `uapfinder-node-<version>-orin-nano.img.xz` from the project's
   GitHub Releases page (`https://github.com/coalsi/uapfinder/releases/latest`)
   and write it to a microSD card (64 GB or larger, A2) with balenaEtcher.
2. Assemble: camera ribbon into **CAM0**, GPS module onto the 40-pin header
   (pins below), optional NVMe SSD, optional ADS-B USB receiver and antenna,
   optional BNO055 orientation sensor, microSD card into the slot under the
   module, Ethernet (recommended) and the power supply.
3. Place the node outdoors with the camera pointing at open sky (anywhere from
   45° up to straight up), and power it on.
4. On a phone or laptop on the same network open **http://uapfinder.local**
   (or join the Wi-Fi network **`UAPFinder-XXXX`**, password **`uapfinder`**,
   if the node has no network, and open **http://10.42.0.1**).
5. Enter the **claim code** the network coordinator sent (format
   `XXXX-XXXX`), set a device password, and use the live preview to check
   the view.
6. Done. The node finds its position and time from GPS, works out exactly
   where the camera points from the stars on the first clear night, and
   appears on the live map.

A one-time firmware update may be needed on brand-new developer kits (see
"Firmware" below); the build guide covers it.

## Hardware conventions

| Item | Connection | Linux device / notes |
|------|------------|----------------------|
| Camera (IMX477 CSI, recommended) | 22-pin ribbon to **CAM0** | `nvarguscamerasrc sensor-id=0`; the image enables the IMX477 camera overlay for CAM0 |
| USB camera (alternative) | any USB 3 port | `/dev/video*` (UVC), auto-detected |
| GPS serial | module TX → header **pin 10** (UART1 RX), module RX → **pin 8** (UART1 TX) | `/dev/ttyTHS1` on JetPack 6 |
| GPS PPS | module PPS → header **pin 7** | `pps-gpio` on PAC.06 via the image's device-tree overlay → `/dev/pps*` (not the timer-based `/dev/pps0`) |
| GPS power | **pin 1** (3.3 V) or **pin 2** (5 V) per the module, **pin 6** (GND) | |
| USB GPS (alternative) | any USB port | `/dev/ttyACM*` / `/dev/ttyUSB*`, no PPS: network time is used instead |
| BNO055 (optional) | **pin 3** (SDA), **pin 5** (SCL), **pin 1** (3.3 V), **pin 9** (GND) | I2C address 0x28 or 0x29; the bus number is discovered by scanning, not assumed |
| ADS-B receiver (optional) | USB (RTL-SDR, VID:PID `0bda:2838` or `0bda:2832`) | the image runs `readsb` when one is present |
| NVMe SSD (optional, recommended) | M.2 Key M slot under the carrier board | formatted automatically if it is blank (no partition table), mounted at `/var/lib/skynet` |

Pin numbers are physical header pins. Anything not plugged in is simply not
used; the status page says what was found.

## Software on the node

All node services run as the unprivileged `skynet` user unless they need
root, and all are enabled in the image and by the command-line installer.

| Unit | What it does |
|------|--------------|
| `uapfinder-firstboot.service` | One-shot on first boot: grow the root partition and file system to the card, set the hostname to `uapfinder-XXXX` (XXXX = last four hex digits of the module serial number, or of the Ethernet MAC), regenerate SSH host keys, format a blank NVMe drive and mount it at `/var/lib/skynet`. |
| `uapfinder-netsetup.service` | If there is no network connection 90 s after boot, start a Wi-Fi hotspot `UAPFinder-XXXX` (WPA2, password `uapfinder`) with the setup page at `http://10.42.0.1`; stop it once the node is on a network. |
| `uapfinder-setup.service` | The setup and status web page on port 80 (`http://uapfinder.local`, also `http://uapfinder-XXXX.local`). |
| `skynet-edge.service` | The detection pipeline (existing). Waits, without failing, until the node is enrolled. |
| `uapfinder-starcal.timer` / `.service` | Automatic pointing calibration from the stars (below). |
| `readsb.service` | Local ADS-B decoding, started only when a receiver is present. |

`skynet-edge autoconfig` (run by the services at boot) detects the hardware,
combines it with the enrollment credentials and the latest calibration, and
writes `/etc/skynet/edge.yaml`. Hand edits go in
`/etc/skynet/edge.local.yaml`, which overrides the generated file.

State lives in `/var/lib/skynet/`: `credentials.json` (0600: node id, key,
server addresses), `calibration.json` (pointing and lens), `clips/`.

Image defaults, written when the image is built, live in
`/etc/skynet/defaults.yaml`; most importantly `enroll_url`, the server the
claim code is sent to. The setup page's advanced section can change it.

### Setup and status page

Port 80, plain HTML + a little JavaScript, works on a phone.

* **Before enrollment** (no device password yet): steps for network (list
  and join Wi-Fi via NetworkManager), claim code and device password.
* **Always**: hardware found (camera, GPS fix and satellites, PPS lock, time
  error from chrony, orientation sensor, ADS-B source, storage), server
  connection, pointing calibration status, software version, a live preview
  (JPEG, about 1 frame/s) with a horizon and elevation overlay for aiming,
  restart and shut-down buttons, and a diagnostics bundle download.
* After the device password is set, everything that changes the node needs
  it; the status view stays readable on the local network.

### Enrollment

The coordinator creates claim codes on the server; the node exchanges one for
its identity.

`POST /api/v1/enrollments` (admin)
```json
{"count": 5, "label": "Phoenix pilot", "expires_in_h": 720}
```
→ `201 {"codes": [{"code": "7KQ2-M9XD", "expires_ns": 1795…}]}`. Codes are
8 characters of Crockford base32 (no I, L, O, U), shown as `XXXX-XXXX`;
input is case-insensitive and ignores the dash.

`POST /api/v1/enroll` (public, rate-limited per client IP like the volunteer
form)
```json
{"code": "7kq2m9xd", "hardware": {"serial": "…", "model": "Jetson Orin Nano", "camera": "imx477", "gps": "pps", "software_version": "0.2.0"}}
```
→ `201`:
```json
{
  "node_id": "…",
  "node_key": "<64 hex>",
  "transport": "udp",
  "udp_host": "mac-studio",
  "udp_port": 9999,
  "https_base_url": "http://mac-studio:8000",
  "tailscale": {"auth_key": "tskey-auth-…", "tags": ["tag:skynet-node"]}
}
```
`tailscale` is null unless the server has `SKYNET_TAILSCALE_AUTHKEY`; when
present, the node installs nothing new (Tailscale is in the image), joins the
tailnet with that key, then reaches the server privately. The transport and
addresses come from `SKYNET_NODE_TRANSPORT`, `SKYNET_NODE_UDP_HOST`,
`SKYNET_NODE_UDP_PORT` and `SKYNET_PUBLIC_URL`. Errors: `404` unknown code,
`410` code already used or expired, `429` rate limited. A code is single use;
the server records which node it produced.

### Pointing calibration from the stars

Accurate pointing matters more than anything else in the error budget
(docs/MATH.md), and an orientation sensor is only good to about 1°. So the
node calibrates itself:

* `skynet_edge.starcal` takes a stacked night-sky frame, the capture time and
  the observer's position, plus a prior (orientation sensor reading, or
  "pointing up, heading unknown"), and solves the camera's yaw, pitch and
  roll, focal length and radial distortion by matching detected stars to a
  bundled bright-star catalog.
* `uapfinder-starcal.timer` runs every 30 minutes; the service exits
  immediately unless the Sun is more than 12° below the horizon, a GPS fix
  exists, and either there is no good calibration yet or the last one is
  more than 7 days old. Clouds or a failed solve just mean it tries again
  later.
* Until the first calibration the node reports with its prior and a large
  angular uncertainty; the server's gate accepts only consistent matches,
  so an uncalibrated node rarely produces false fixes. The status page shows
  "Pointing: waiting for a clear night" until then.

Interface (owned by the star-calibration module, called by the pipeline):

```python
from skynet_edge.starcal import Observer, Prior, StarCalResult, calibrate, is_dark_enough

result: StarCalResult = calibrate(image, t_ns, Observer(lat, lon, alt_m), Prior(...))
# image: 2-D numpy array (uint8, uint16 or float), a stack of several frames
# result.ok, result.reason, result.yaw_deg, result.pitch_deg, result.roll_deg,
# result.fx, result.fy, result.cx, result.cy, result.k1, result.k2,
# result.stars_matched, result.rms_deg, result.t_ns
is_dark_enough(t_ns, Observer(...)) -> bool   # Sun altitude below -12°
```

`Prior` fields: `width`, `height`, `fov_h_deg` (nominal), and optional
`yaw_deg`, `pitch_deg`, `roll_deg` with `sigma_deg` (None = unknown).

### Time

chrony uses the GPS PPS pulse with the GPS NMEA time when a PPS device is
present, and network time otherwise; the status page shows chrony's
estimated error and warns above 2 ms, which is where timing starts to
matter for fast objects.

## The OS image

* Base: NVIDIA Jetson Linux **R36.5.2** (JetPack 6.2.3, Ubuntu 22.04) for the
  Jetson Orin Nano developer kit, built with NVIDIA's BSP and
  `jetson-disk-image-creator.sh`, customised in a chroot.
* Contents: the BSP's camera, GStreamer and multimedia stack; the UAP Finder
  node software in `/opt/skynet/venv` (installed by
  `edge/deploy/install-node.sh --chroot`); gpsd, chrony, NetworkManager,
  avahi, Tailscale, readsb, ffmpeg; the `pps-gpio` kernel module and the
  device-tree overlays for PPS on pin 7 and the IMX477 on CAM0; all units
  above enabled. No desktop. NVIDIA's first-boot configuration wizard is
  skipped (a default `uapfinder` account exists with SSH disabled until the
  device password is set in the setup page).
* The root filesystem is NVIDIA's *minimal* flavor (`nv_build_samplefs.sh
  --flavor minimal`), so the compressed image stays well under the release
  limit. The overlays are not merged into NVIDIA's boot entry: the image
  adds a separate `uapfinder` entry to `/boot/extlinux/extlinux.conf` (the
  stock `primary` entry stays as the fallback in the boot menu). Writing it
  needs the running board's identity, so `uapfinder-overlays.service` does
  it on the first boot and **reboots once** before the node's own services
  start; `touch /etc/uapfinder/overlays.disabled` reverses it. readsb is
  installed disabled and started by a udev rule when an RTL-SDR is plugged
  in; tailscaled is disabled until enrollment returns an auth key.
* Built by `.github/workflows/node-image.yml` on GitHub-hosted runners
  (workflow_dispatch and tags `node-image-v*`), published as
  `uapfinder-node-<version>-orin-nano.img.xz` plus its SHA-256 to a GitHub
  Release. GitHub limits release files to 2 GiB, so the image must stay
  lean. The enrollment server baked into the image comes from the workflow
  input / repository variable `UAPF_ENROLL_URL`.
* The command-line installer (`edge/deploy/jetson/setup.sh`, which calls
  `install-node.sh`) produces the same system on a Jetson that already runs
  JetPack 6, for people who prefer it.

### Firmware

Developer kits that left the factory with JetPack 5 firmware cannot boot a
JetPack 6 card until their firmware is updated once with NVIDIA's own
JetPack 5.1.3 SD card image (NVIDIA's getting-started guide). The build guide
explains how to tell and how to do it. Kits already on 36.x firmware are
brought to the card's 36.5.2 firmware automatically on its first boot by
NVIDIA's bootloader package (one extra restart; do not cut power).

## Not yet verified on hardware

The image build, the services and the setup page are tested in CI and in
chroot/containers; booting the image on a real Orin Nano, the CSI camera
overlay, PPS on pin 7 and the hotspot need a real device. The guide and the
site say so.
