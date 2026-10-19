# Building a SKY-NET node image (Jetson Orin Nano, JetPack 6.x)

> **Status: not executed by the authors.** These steps follow NVIDIA's
> Jetson Linux r36 (JetPack 6.2) documentation and forum guidance as of
> October 2026, but no image was built or flashed while writing them. Try
> one unit before a batch, and check the Jetson Linux release notes for the
> current 36.x version.

There are two ways to get a node running. Path A needs nothing but a Mac
(or any PC) and a microSD card; path B produces a reproducible image for
many nodes.

## Path A: NVIDIA's SD-card image + `setup.sh` (recommended for the first nodes)

1. **Firmware check.** JetPack 6 needs Jetson UEFI firmware 36.x in the
   module's QSPI flash. Kits shipped before JetPack 6 have 35.x firmware
   and will not boot a JetPack 6 card. The firmware version is shown on the
   NVIDIA splash screen at power-on (or `sudo nvbootctrl dump-slots-info`
   on a running system). If it is older than 36.0:
   1. Write the **JetPack 5.1.3** SD-card image (use NVIDIA's re-issued
      2024-05-28 image), boot it once and finish the setup wizard.
   2. `sudo apt-get install nvidia-l4t-jetson-orin-nano-qspi-updater`,
      then reboot: the QSPI update runs during the reboot (it can take
      several minutes; do not cut power).
   3. Power off (a real power cycle, not `reboot`), swap in the JetPack 6
      card.
2. **Write the JetPack 6.2 (or newer 6.x) "Jetson Orin Nano Developer Kit SD
   Card Image"** with [balenaEtcher](https://etcher.balena.io/) — it runs on
   macOS: *Flash from file* -> the downloaded `.zip` -> the microSD card ->
   *Flash*. Use a 64 GB or larger UHS-I card (or NVMe, see path B; clips
   should go to NVMe anyway, raw export runs at ~190 MB/s at 1080p60).
3. Boot, complete the first-boot wizard (user, network, time zone), then:
   ```bash
   sudo apt-get update && sudo apt-get install -y git
   git clone <this repository> uapfinder && cd uapfinder
   sudo SKYNET_NODE_ID=node-a SKYNET_NODE_KEY=<64 hex from POST /api/v1/nodes> \
        SKYNET_SERVER_HOST=mac-studio TS_AUTHKEY=tskey-auth-... \
        ./edge/deploy/jetson/setup.sh
   ```
   See [`../jetson/setup.sh`](../jetson/setup.sh) for every variable.
4. **Power mode.** List the modes with `sudo nvpmodel -q --verbose`. On an
   Orin Nano running JetPack 6.2 with the *Super* configuration, `MAXN_SUPER`
   is reported as mode 2 (NVIDIA's JetPack 6.2 announcement), alongside
   15 W and 25 W modes; the 67 TOPS figure applies to that mode. Set it with
   `SKYNET_NVPMODEL=<id>` when running `setup.sh`, or `sudo nvpmodel -m <id>`.
   Make sure the power supply and cooling are rated for it.

## Path B: custom image from the L4T BSP

Host: x86_64 Ubuntu 22.04 with ~40 GB free (or NVIDIA's "Jetson Linux Flash"
container), `qemu-user-static` and `binfmt-support` installed, and a USB-C
data cable to the devkit.

```bash
L4T=36.4.3                      # JetPack 6.2; use the current r36 release
# From the Jetson Linux R36.x release page (developer.nvidia.com/embedded/jetson-linux):
#   Jetson_Linux_R${L4T}_aarch64.tbz2                      (the BSP: Linux_for_Tegra/)
#   Tegra_Linux_Sample-Root-Filesystem_R${L4T}_aarch64.tbz2 (Ubuntu 22.04 sample rootfs)
tar xf Jetson_Linux_R${L4T}_aarch64.tbz2
sudo tar xpf Tegra_Linux_Sample-Root-Filesystem_R${L4T}_aarch64.tbz2 -C Linux_for_Tegra/rootfs/
cd Linux_for_Tegra
sudo ./tools/l4t_flash_prerequisites.sh      # host packages for flashing
sudo ./apply_binaries.sh                     # NVIDIA userspace + apt source into rootfs/

# SKY-NET packages and software into the rootfs (chroot via qemu), plus a
# one-shot unit that runs setup.sh on first boot. firstboot.env holds
# SKYNET_NODE_ID, SKYNET_NODE_KEY, SKYNET_SERVER_HOST, TS_AUTHKEY, ...
# (one image per node, or leave the key out and add it later).
sudo /path/to/uapfinder/edge/deploy/image/customize_rootfs.sh \
     "$PWD/rootfs" /path/to/uapfinder /path/to/firstboot.env

# Default user, so the first boot needs no keyboard or screen (oem-config skipped).
sudo ./tools/l4t_create_default_user.sh -u skynet-admin -p '<strong password>' -n node-a --accept-license
```

Flash: put the devkit in **recovery mode** (with power off, jumper the FC REC
pin to GND on the carrier's button header, as shown in the devkit user guide,
then power on; `lsusb` on the host shows an "NVIDIA Corp. APX" device),
connect USB-C, and run from `Linux_for_Tegra/`:

```bash
# NVMe SSD (recommended), Orin Nano devkit with the Super power modes:
sudo ./tools/kernel_flash/l4t_initrd_flash.sh --external-device nvme0n1p1 \
     -c tools/kernel_flash/flash_l4t_t234_nvme.xml \
     -p "-c bootloader/generic/cfg/flash_t234_qspi.xml" \
     --showlogs --network usb0 jetson-orin-nano-devkit-super internal
```

* `jetson-orin-nano-devkit-super` exists from r36.4.3 (JetPack 6.2); on
  older BSPs use `jetson-orin-nano-devkit` (no Super modes).
* This also writes the QSPI firmware, so the JetPack 5.1.3 step of path A is
  not needed.
* For a microSD card instead of NVMe, use `--external-device mmcblk0p1` and
  check the BSP's Quick Start for the matching layout file; or simply use
  path A.
* Users report initrd flashing occasionally stalling at "Waiting for target
  to boot-up"; a different USB port/cable, or SDK Manager, usually helps.

On first boot `skynet-firstboot.service` runs `setup.sh` (udev, gpsd,
chrony with PPS detection, Tailscale join, the `skynet-edge` service), then
deletes `/etc/skynet/firstboot.env`. Follow with the calibration steps in
[`edge/README.md`](../../README.md).

## Sources

* NVIDIA, "Software Encode in Orin Nano" (Orin Nano has no NVENC):
  https://docs.nvidia.com/jetson/archives/r36.3/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html
* NVIDIA, Jetson Orin Nano Developer Kit user guide, "JetPack 6.x update path":
  https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/update_firmware.html
* NVIDIA, Jetson Linux r36.4.3 Quick Start and Flashing Support:
  https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/IN/QuickStart.html
* NVIDIA blog, "JetPack 6.2 brings Super mode to Jetson Orin Nano and Orin NX":
  https://developer.nvidia.com/blog/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules
* RidgeRun, "Migrating from JetPack 5 to JetPack 6 on Orin Nano without a Linux host":
  https://developer.ridgerun.com/wiki/index.php/JetPack_6_Migration_and_Developer_Guide/Migrating_from_JetPack_5_to_JetPack_6/Upgrade_Orin_Nano_without_a_Linux_host
