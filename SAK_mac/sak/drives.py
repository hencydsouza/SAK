"""External drive discovery and FAT32 formatting (replaces DriveGetDrive + guiformat.exe)."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .common import Job, SakError
from .tools import find_tool, run

WINDOWS = sys.platform == "win32"


@dataclass
class Disk:
    identifier: str  # e.g. disk4
    name: str
    size: int
    mount_points: list[str]
    internal: bool


def _diskutil_plist(verb: str, *args: str) -> dict:
    res = subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True)
    if res.returncode != 0:
        raise SakError(f"diskutil {verb} failed: {res.stderr.decode(errors='replace').strip()}")
    return plistlib.loads(res.stdout)


def _windows_removable() -> list[Disk]:
    """SAK's DriveGetDrive($DT_REMOVABLE): removable drive letters (SD readers, USB sticks)."""
    import ctypes

    k32 = ctypes.windll.kernel32
    disks = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if k32.GetDriveTypeW(root) != 2:  # DRIVE_REMOVABLE
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261):
            continue  # empty card reader slot
        try:
            size = shutil.disk_usage(root).total
        except OSError:
            size = 0
        name = f"{label.value or 'Removable'} ({fs.value})" if fs.value else (label.value or "Removable")
        disks.append(Disk(root[:2], name, size, [root], False))
    return disks


def external_disks() -> list[Disk]:
    """Physical external disks (USB sticks, SD card readers) with their mounted volumes."""
    if WINDOWS:
        return _windows_removable()
    listing = _diskutil_plist("list", "external", "physical")
    disks = []
    for entry in listing.get("AllDisksAndPartitions", []):
        ident = entry["DeviceIdentifier"]
        info = _diskutil_plist("info", ident)
        if info.get("Internal", False):
            continue
        mounts = [p["MountPoint"] for p in entry.get("Partitions", []) if p.get("MountPoint")]
        if entry.get("MountPoint"):
            mounts.append(entry["MountPoint"])
        disks.append(Disk(ident, info.get("MediaName", ident), int(info.get("TotalSize", 0)), mounts, False))
    return disks


def removable_volumes() -> list[str]:
    """Mount points usable as split targets (SAK's 'split to USB/SD' combo)."""
    return [m for d in external_disks() for m in d.mount_points]


def format_fat32(identifier: str, label: str, job: Job, confirm: str, cluster_64k: bool = False) -> None:
    """ERASE an external disk to MBR + one FAT32 partition.

    `confirm` must repeat the identifier - this wipes the whole disk.
    cluster_64k reformats the partition with 64 KiB clusters (recommended for Switch);
    that step uses newfs_msdos on the raw device and needs root.
    """
    if WINDOWS:
        _open_guiformat(job)
        return
    identifier = identifier.removeprefix("/dev/")
    if not re.fullmatch(r"disk\d+", identifier):
        raise SakError("disk identifier must look like disk4 (a whole disk)")
    if confirm != identifier:
        raise SakError(f"refusing to erase: confirmation {confirm!r} != {identifier!r}")
    label = re.sub(r"[^A-Z0-9_]", "", label.upper())[:11] or "SWITCH"
    disk = next((d for d in external_disks() if d.identifier == identifier), None)
    if disk is None:
        raise SakError(f"{identifier} is not an external physical disk - refusing to format")
    if cluster_64k and os.geteuid() != 0:
        raise SakError("64 KiB cluster reformat needs root: re-run with sudo")
    code, _ = run(["diskutil", "eraseDisk", "FAT32", label, "MBRFormat", f"/dev/{identifier}"], job)
    if code != 0:
        raise SakError("diskutil eraseDisk failed")
    if cluster_64k:
        part = f"{identifier}s1"
        run(["diskutil", "unmountDisk", f"/dev/{identifier}"], job)
        code, _ = run(["newfs_msdos", "-F", "32", "-c", "128", "-v", label, f"/dev/r{part}"], job)
        if code != 0:
            raise SakError("newfs_msdos failed")
        run(["diskutil", "mountDisk", f"/dev/{identifier}"], job)
    job.log(f"{identifier} formatted as FAT32 ({label})")


def _open_guiformat(job: Job) -> None:
    """Windows: SAK just launched bin\\guiformat.exe; it picks the drive and confirms the erase itself."""
    tool = find_tool("guiformat")
    subprocess.Popen([str(tool)], close_fds=True)
    job.log(f"opened {tool.name}: choose the SD card, allocation unit size (64K for Switch) and press Start there")
