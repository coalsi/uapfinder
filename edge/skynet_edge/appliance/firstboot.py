"""Decision helpers for ``edge/deploy/appliance/uapfinder-firstboot.sh`` (root).

``python3 -m skynet_edge.appliance.firstboot <command>``:

* ``suffix``: the 4 hex digits for ``uapfinder-XXXX``: the last four hex
  digits of the module serial number (device tree ``serial-number``), else
  of the first wired Ethernet MAC address, else of ``/etc/machine-id``.
* ``root-partition``: ``<disk> <partition number> <partition device>`` of the
  file system mounted on ``/`` (found through its device number, so it also
  works when ``/proc/mounts`` says ``/dev/root``).
* ``nvme-blank <device>``: exit 0 only if the whole disk carries no partition
  table and no file-system signature and is not in use; prints the reason
  otherwise (exit 1). The disk is never written by this module.

All of these read sysfs/procfs under configurable roots, for the tests.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from typing import List, Optional, Tuple

from .util import CmdResult, read_text, run

SECTOR = 512


def _read(path: str) -> Optional[str]:
    t = read_text(path, 4096)
    return None if t is None else t.strip().strip("\x00").strip()


def hex_tail(text: Optional[str], n: int = 4) -> Optional[str]:
    if not text:
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", text)
    return digits[-n:].lower() if len(digits) >= n else None


def hostname_suffix(sys_root: str = "/sys", proc_root: str = "/proc", etc_root: str = "/etc") -> str:
    serial = _read(f"{proc_root}/device-tree/serial-number") or _read(f"{sys_root}/firmware/devicetree/base/serial-number")
    tail = hex_tail(serial)
    if tail:
        return tail
    for iface in sorted(glob.glob(f"{sys_root}/class/net/*")):
        if not os.path.exists(f"{iface}/device") or os.path.exists(f"{iface}/wireless") \
                or os.path.exists(f"{iface}/phy80211"):
            continue  # virtual or wireless
        if _read(f"{iface}/type") != "1":
            continue
        mac = _read(f"{iface}/address")
        tail = hex_tail(mac)
        if tail and mac != "00:00:00:00:00:00":
            return tail
    return hex_tail(_read(f"{etc_root}/machine-id")) or "0000"


def root_partition(sys_root: str = "/sys", st_dev: Optional[int] = None) -> Tuple[str, int, str]:
    dev = os.stat("/").st_dev if st_dev is None else st_dev
    link = f"{sys_root}/dev/block/{os.major(dev)}:{os.minor(dev)}"
    real = os.path.realpath(link)
    part = _read(f"{real}/partition")
    if part is None or not part.isdigit():
        raise RuntimeError(f"/ is not on a partition ({real})")
    disk = os.path.basename(os.path.dirname(real))
    return f"/dev/{disk}", int(part), f"/dev/{os.path.basename(real)}"


def _mounted_sources(proc_root: str) -> List[str]:
    text = read_text(f"{proc_root}/mounts", 1 << 20) or ""
    srcs = [line.split()[0] for line in text.splitlines() if line.split()]
    swaps = read_text(f"{proc_root}/swaps", 1 << 16) or ""
    srcs += [line.split()[0] for line in swaps.splitlines()[1:] if line.split()]
    return srcs


def raw_signatures(head: bytes, tail: bytes) -> List[str]:
    """Partition-table / superblock magics in the first and last bytes of a disk (belt and braces for blkid)."""
    found = []
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        found.append("MBR/boot signature")
    if len(head) >= 520 and head[512:520] == b"EFI PART":
        found.append("GPT header")
    if b"EFI PART" in tail:
        found.append("backup GPT header")
    if len(head) >= 1082 and head[1080:1082] == b"\x53\xef":
        found.append("ext2/3/4 superblock")
    if len(head) >= 0x10040 and head[0x10040:0x10048] == b"_BHRfS_M":
        found.append("btrfs superblock")
    if head[:4] == b"XFSB":
        found.append("XFS superblock")
    if len(head) >= 4096 + 10 and head[4086:4096] in (b"SWAPSPACE2", b"SWAP-SPACE"):
        found.append("swap signature")
    if head[3:11] in (b"NTFS    ", b"EXFAT   ", b"MSDOS5.0", b"mkfs.fat") or head[82:87] == b"FAT32":
        found.append("FAT/NTFS/exFAT boot sector")
    if head[:4] == b"LUKS":
        found.append("LUKS header")
    return found


def nvme_blank(device: str, sys_root: str = "/sys", proc_root: str = "/proc",
               runner=run, reader=None) -> Tuple[bool, str]:
    name = os.path.basename(device)
    if not re.fullmatch(r"nvme\d+n\d+", name):
        return False, "not an NVMe namespace device"
    blk = f"{sys_root}/block/{name}"
    if not os.path.isdir(blk):
        return False, "no such disk"
    parts = glob.glob(f"{blk}/{name}p*")
    if parts:
        return False, f"has {len(parts)} partition(s)"
    if os.path.isdir(f"{blk}/holders") and os.listdir(f"{blk}/holders"):
        return False, "in use (holders)"
    if device in _mounted_sources(proc_root):
        return False, "mounted or used as swap"
    res: CmdResult = runner(["blkid", "-p", "-o", "export", device], timeout=10.0)
    if res.returncode == 0:
        return False, "blkid found: " + " ".join(res.stdout.split())
    if res.returncode != 2:
        return False, f"blkid could not probe it (exit {res.returncode})"
    res = runner(["wipefs", "--no-act", "--noheadings", device], timeout=10.0)
    if res.returncode != 0:
        return False, f"wipefs could not probe it (exit {res.returncode})"
    if res.stdout.strip():
        return False, "wipefs found signatures"
    size = _read(f"{blk}/size")
    if not size or not size.isdigit() or int(size) < 2048:
        return False, "unknown or tiny size"
    head, tail = (reader or _read_ends)(device, int(size) * SECTOR)
    sigs = raw_signatures(head, tail)
    if sigs:
        return False, "found " + ", ".join(sigs)
    return True, "blank: no partition table, no file-system signature"


def _read_ends(device: str, size: int) -> Tuple[bytes, bytes]:
    with open(device, "rb") as f:
        head = f.read(1 << 20)
        f.seek(max(0, size - (1 << 20)))
        tail = f.read(1 << 20)
    return head, tail


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: firstboot {suffix|root-partition|nvme-blank DEVICE}", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "suffix":
        print(hostname_suffix())
        return 0
    if cmd == "root-partition":
        disk, num, dev = root_partition()
        print(f"{disk} {num} {dev}")
        return 0
    if cmd == "nvme-blank" and len(args) == 2:
        ok, why = nvme_blank(args[1])
        print(why)
        return 0 if ok else 1
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
