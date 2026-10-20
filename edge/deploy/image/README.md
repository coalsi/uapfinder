# UAP Finder node image (Jetson Orin Nano developer kit, microSD card)

One file you write to a microSD card; the node then sets itself up
(docs/NODE_APPLIANCE.md). Built by `.github/workflows/node-image.yml` from
NVIDIA's **Jetson Linux R36.5.2 (JetPack 6.2.3, Ubuntu 22.04)**.

> **Hardware status: not hardware-tested.** The build scripts, the root
> file-system customisation and the image checks run in CI and in an
> arm64 chroot under qemu on an x86_64 host. Nobody has booted this image on
> an Orin Nano yet. Booting, the CSI camera overlay, PPS on header pin 7,
> the pps-gpio module, the hotspot and the first-boot reboot all need a
> real device. Try one card before a batch.

## For volunteers

1. **Download** `uapfinder-node-<version>-orin-nano.img.xz` (and, if you
   want to check it, `SHA256SUMS`) from
   <https://github.com/coalsi/uapfinder/releases/latest>.
   Optional check: `sha256sum -c SHA256SUMS` (macOS:
   `shasum -a 256 -c SHA256SUMS`).
2. **Write it** with [balenaEtcher](https://etcher.balena.io/) (macOS,
   Windows, Linux): *Flash from file* -> the `.img.xz` (no need to unpack
   it) -> your microSD card (64 GB or larger, A2) -> *Flash*.
3. Put the card in the slot under the module, connect the parts, power on.
   The **first boot restarts once by itself** (about a minute in) to switch
   on the camera and GPS-pulse settings, and grows the system to fill the
   card. Then open <http://uapfinder.local> (or join the Wi-Fi network
   `UAPFinder-XXXX`, password `uapfinder`, and open <http://10.42.0.1>).

**Firmware (once per developer kit).** A JetPack 6 card only boots on a kit
whose firmware is already 36.x. Kits that left the factory with JetPack 5
firmware show nothing useful on screen with this card (or stop at the NVIDIA
logo). Update them once with NVIDIA's own **JetPack 5.1.3** SD card image as
NVIDIA's getting-started guide describes
(<https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/update_firmware.html>):
write that image, boot it, let the firmware update finish, power off, then
use this card. Kits already on 36.x firmware are updated to the matching
36.5.2 firmware automatically on the first boot of this card (NVIDIA's
`nvidia-l4t-bootloader` package does this; it takes a few extra minutes and
one extra restart; do not cut the power).

**Camera.** The image switches on NVIDIA's IMX477 overlay for the **CAM0**
connector, which uses NVIDIA's built-in IMX477 driver. Whether a given
Arducam IMX477 board works with it or needs Arducam's own driver package is
not verified here; the Raspberry Pi HQ camera is reported to need its
resistor R8 removed on Jetson. Neither is hardware-tested.

**If a node does not boot after the first restart**, the camera/GPS
settings are the likely cause. With a monitor and keyboard (or the serial
console) choose **`primary`** in the boot menu that appears for a few
seconds; that is NVIDIA's unchanged entry. Then report it. Writing the card
again also restores everything.

## For maintainers

### What the image contains

* NVIDIA's **minimal** Ubuntu 22.04 root filesystem (built with the BSP's
  `tools/samplefs/nv_build_samplefs.sh --flavor minimal`: no desktop, no
  first-boot wizard), plus NVIDIA's L4T packages (`apply_binaries.sh`:
  kernel, firmware, camera/Argus, GStreamer and multimedia stack).
* `packages.txt`: NetworkManager, avahi, chrony, gpsd, pps-tools, ffmpeg,
  GStreamer tools, device-tree-compiler, growpart and friends.
  `packages-remove.txt` is purged (a guard keeps anything whose removal
  would take NVIDIA packages with it).
* **Tailscale** from its official apt repository (`install-tailscale.sh`),
  `tailscaled` disabled until enrollment hands the node an auth key.
* **readsb** built from the pinned upstream tag as a Debian package
  (`install-readsb.sh`; upstream has no apt repository), disabled; a udev
  rule starts it when an RTL-SDR (`0bda:2838`/`0bda:2832`) is plugged in.
* The node software via **`edge/deploy/install-node.sh --chroot`**, which
  calls **`edge/deploy/platform/install-platform.sh`** (run by the build
  directly if the installer did not).
* `/etc/skynet/defaults.yaml` with `enroll_url`; `/etc/uapfinder-release`.
* The default account `uapfinder` (created by NVIDIA's
  `l4t_create_default_user.sh`, which also skips NVIDIA's oem-config
  wizard) with its **password locked**; SSH disabled. The setup page
  (install-node.sh's software) sets the device password and enables SSH. No SSH host keys and an empty
  machine-id in the image (each card gets its own).
* Boots to `multi-user.target`.

### Platform: PPS and camera (`edge/deploy/platform/`)

* **pps-gpio module.** `install-platform.sh` checks the L4T kernel's
  `modules.builtin` and module tree. If `pps-gpio` is neither built in nor a
  module but the PPS core is there, it builds `pps-gpio/pps-gpio.c`
  (vendored from Linux 5.15.148, one log line changed so it also builds on
  later 5.15.y kernels where `struct pps_device` embeds its `struct device`)
  out of tree against
  `/lib/modules/<kver>/build` (`nvidia-l4t-kernel-headers`) in the chroot
  and installs it to `/lib/modules/<kver>/updates/uapfinder/`. Without the
  PPS core it warns, and the node uses GPS serial / network time.
* **Overlays.** `overlays/uapfinder-pps-pin7.dts` adds a `pps-gpio` node on
  `TEGRA234_MAIN_GPIO(AC, 6)` = 166 (header pin 7, `soc_gpio59_pac6`) and a
  pin configuration (input, no pull) that only that node uses: it changes
  nothing that exists, so a mistake can only make the PPS device fail to
  probe. The camera uses NVIDIA's `tegra234-p3767-camera-p3768-imx477-A.dtbo`
  (CSI port A = CAM0; `-imx477-dual.dtbo` if `-A` is missing).
* **Safe registration.** L4T's UEFI applies `OVERLAYS` only on top of an
  `FDT` named in the same extlinux entry. `configure-overlays.sh`
  (`uapfinder-overlays.service`, every boot, before the node's services)
  picks the kernel DTB whose compatible matches the running board, checks
  each overlay with `fdtoverlay` against it, and writes a separate
  **`uapfinder`** entry (copy of `primary` + `FDT` + `OVERLAYS`) between
  marker comments, then points `DEFAULT` at it. `primary` is never changed
  and stays in the menu. On the first boot this needs one reboot, which the
  service does itself (once per configuration, never in a loop). If
  `DEFAULT` names something else (e.g. a jetson-io entry) the file is left
  alone. Reverse it with `touch /etc/uapfinder/overlays.disabled` and a
  reboot. The build also runs `check-overlays.sh`, which merges the overlays
  into the BSP's p3767-0005 kernel DTBs and fails the build if they do not
  apply.
* **Root partition growth.** On R36 NVIDIA's `nvresizefs` grows the root
  file system on first boot. The node's `uapfinder-firstboot.service` does
  it too (a no-op when already done); a drop-in orders it after NVIDIA's
  first-boot units and `uapfinder-overlays.service`, so they never run at
  the same time.

### Running the workflow

Actions tab -> **Node image** -> *Run workflow*:

| Input | Meaning |
|-------|---------|
| `version` | e.g. `0.2.0`; the file is `uapfinder-node-0.2.0-orin-nano.img.xz` |
| `enroll_url` | enrollment server written to `/etc/skynet/defaults.yaml`; empty uses the repository variable **`UAPF_ENROLL_URL`** (Settings -> Secrets and variables -> Actions -> Variables). The run fails if both are empty. |
| `publish` | also create/update the Release `node-image-v<version>` |

Or push a tag: `git tag node-image-v0.2.0 && git push origin node-image-v0.2.0`
(builds and publishes, enroll URL from the repository variable).

Every run uploads the `.img.xz` and `SHA256SUMS` as a workflow artifact
(14 days). The run fails if the compressed image is 2 GiB or larger
(GitHub's per-file release limit). The first run builds the minimal root
filesystem under qemu (about an hour) and caches it with the BSP download;
later runs with the same `versions.env` reuse both. Expect 1.5-3 hours.

### Building locally (x86_64 Ubuntu 22.04)

About 40 GB free, root, and network access to NVIDIA, Ubuntu, Tailscale
and GitHub:

```bash
sudo apt-get install -y qemu-user-static binfmt-support
sudo edge/deploy/image/build-image.sh --version 0.2.0-dev \
     --enroll-url https://your-server.example \
     --work /data/uapf/work --cache /data/uapf/cache --out /data/uapf/out
```

Steps: `host download extract rootfs binaries overlays user customize
checkroot image checkimage compress`. Each leaves a stamp; re-running
continues where it stopped, `--from customize` redoes from a step,
`--only-through rootfs` stops early, `--keep-raw` keeps the `.img`.

Check an image without booting it (loop-mounts the APP partition):

```bash
sudo edge/deploy/image/test-image.sh out/uapfinder-node-0.2.0-dev-orin-nano.img.xz \
     --expect-version 0.2.0-dev --expect-enroll-url https://your-server.example
```

Test the customisation without NVIDIA's files (plain Ubuntu arm64 base in a
qemu chroot, a stub node installer, a fake GPT image):

```bash
curl -fLO https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz
sudo edge/deploy/image/tests/local-chroot-test.sh ubuntu-base-22.04.5-base-arm64.tar.gz /tmp/uapf-test
#   --real-install-node to use edge/deploy/install-node.sh instead of the stub
```

### Changing the base version

Everything version-specific is in `versions.env`:

1. Read the new release's notes (Orin Nano devkit support, Ubuntu release,
   the SD-card flow of `jetson-disk-image-creator.sh`).
2. Set `L4T_VERSION`, `JETPACK_VERSION`, the `r36_release_vX.Y` parts of
   `L4T_URL_BASES` and the file names. NVIDIA publishes only SHA-1
   (`release_sha_hashes.txt` next to the files): put those in `BSP_SHA1`
   / `ROOTFS_SHA1`, download the BSP once and pin `BSP_SHA256`
   (`sha256sum`).
3. Run the workflow without publishing, flash the artifact, check on a
   device, then publish.

A new major release (R38/R39) may move to another Ubuntu release and
changes `nv_build_samplefs.sh` and the image creator; review
`build-image.sh` steps `rootfs` and `image`.

### Image size

The minimal rootfs + L4T packages + node software is a few GB in use;
`jetson-disk-image-creator.sh` sizes the APP partition to the content plus
10 %, and the unused blocks are zeros, so `xz -9` brings the image to an
expected **0.8-1.5 GB** (estimate; the real number is in each run's
summary). If it ever approaches 2 GiB: keep `ROOTFS_SOURCE=minimal`, move
large optional parts (CUDA/TensorRT, which this image does not include) to
first-boot downloads, trim `packages.txt`.

### What is not hardware-tested

Booting the image; the `-super` board configuration with this BSP's image
creator (the build falls back to `jetson-orin-nano-devkit` and says so if
the creator does not know it); the automatic firmware update on first boot;
the first-boot reboot; the overlays on a real board (they are only checked
with `fdtoverlay`); pps-gpio on pin 7 (and whether pin 7's pad needs more
than the GPIO request to become an input); the IMX477 on CAM0 being CSI
port A; the out-of-tree pps-gpio build against `nvidia-l4t-kernel-headers`
(built locally only against Ubuntu's 5.15.0-198 arm64 headers); readsb with a
real receiver.

## Sources

* NVIDIA Jetson Linux R36.5.2 release page and notes:
  <https://developer.nvidia.com/embedded/jetson-linux-r3652>,
  <https://docs.nvidia.com/jetson/archives/r36.5.2/ReleaseNotes/Jetson_Linux_Release_Notes_r36.5.2.pdf>;
  JetPack 6.2.3 announcement:
  <https://forums.developer.nvidia.com/t/jetpack-6-2-3-jetson-linux-36-5-2-is-now-live/379873>.
* Download URLs and hashes as used by other R36.5.2 builds:
  seiggy/lucia-dotnet `infra/appliance/release/appliance.lock` (SHA-256 of
  BSP and sample rootfs), avocado-linux/avocado-os
  `bsp/recomputer-mini-j5012/PROVENANCE.md` (same BSP SHA-256, upper-case
  URL), fatihaybsn/Orin-Stage `catalog/data/targets/jp6.2.3.yaml` (NVIDIA's
  SHA-1 from `release_sha_hashes.txt`).
* NVIDIA R36 Flashing Support (SD card image, `jetson-disk-image-creator.sh
  -b jetson-orin-nano-devkit -d SD`, `l4t_create_default_user.sh`):
  <https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/FlashingSupport.html>;
  the script itself (sizes the APP partition to the rootfs + 10 %).
* andyattebery/jetson-orin-nano-l4t-minimal (minimal-flavor SD image in
  GitHub Actions; `-d SD` required, SKU 0005 only, nvresizefs up to R36.5,
  automatic QSPI update by `nvidia-l4t-bootloader`).
* Overlays: JetsonHacks "Device Tree Overlays on Jetson" and
  jetsonhacks/jetson-orin-gpio-patch (pin 7 = `soc_gpio59_pac6`);
  NVIDIA/jetson-gpio `gpio_pin_data.py` (pin 7 = PAC.06);
  `include/dt-bindings/gpio/tegra234-gpio.h` (port AC = 20);
  NVIDIA's `tegra234-p3767-0000-common.h` (`JETSON_COMPATIBLE_P3768`);
  extlinux `OVERLAYS` comma-separated and requiring `FDT`
  (andy60306637/tegra-demo-distro guide, ARK-Electronics/ark_jetson_kernel
  `docs/camera_overlays.md`).
* readsb Debian package build: <https://github.com/wiedehopf/readsb#debian-package>.
* Tailscale for Ubuntu 22.04: <https://pkgs.tailscale.com/stable/#ubuntu-jammy>.
