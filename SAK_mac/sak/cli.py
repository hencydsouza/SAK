"""Command line front-end: `python -m sak <command> ...`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, drives, extract, forwarder, nacp, ops, split, tools
from .common import Cancelled, Job, SakError


def _job(quiet: bool) -> Job:
    def progress(pct: int) -> None:
        if not quiet:
            print(f"\r{pct:3d}%", end="", file=sys.stderr, flush=True)

    def log(msg: str) -> None:
        print(("\r" if not quiet else "") + msg, flush=True)

    return Job(on_progress=progress, on_log=log)


def _each(paths, fn):
    results = []
    for p in paths:
        r = fn(Path(p))
        results.extend(r if isinstance(r, list) else [r])
    return results


def cmd_doctor(a, job):
    ok = True
    for name in tools.NATIVE_TOOLS:
        try:
            print(f"[ok]      {name}: {tools.find_tool(name)}")
        except SakError as e:
            ok = False
            print(f"[missing] {name}: {e}")
    try:
        print(f"[ok]      squirrel: {' '.join(tools.squirrel_cmd())}")
    except SakError as e:
        ok = False
        print(f"[missing] squirrel: {e}")
    try:
        print(f"[ok]      keys: {tools.find_keys(a.keys)}")
    except SakError as e:
        ok = False
        print(f"[missing] keys: {e.args[0].splitlines()[0]}")
    return [] if ok else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sak", description=f"Switch Army Knife for macOS {__version__}")
    p.add_argument("--keys", help="prod.keys / keys.dat (default: SAK_mac/keys, ~/.switch, $SAK_KEYS)")
    p.add_argument("-o", "--out", default=".", help="output folder (default: current folder)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name, help_, fn, files=True, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        if files:
            sp.add_argument("files", nargs="+", type=Path)
        sp.set_defaults(fn=fn)
        return sp

    k = lambda a: tools.find_keys(a.keys)  # noqa: E731
    out = lambda a: Path(a.out)  # noqa: E731

    sp = add("split-xci", "split XCI into FAT32-sized .xc0/.xc1 parts",
             lambda a, j: _each(a.files, lambda f: split.split_xci(f, out(a), j, trim=a.trim)))
    sp.add_argument("--trim", action="store_true", help="drop 0xFF padding blocks from parts after the first")
    add("merge-xci", "merge .xc0 / .xci.00 / 00 parts", lambda a, j: _each(a.files, lambda f: split.merge_xci(f, out(a), j)))
    add("split-nsp", "split NSP into an archive-bit folder", lambda a, j: _each(a.files, lambda f: split.split_nsp(f, out(a), j)))
    add("merge-nsp", "merge a split NSP folder", lambda a, j: _each(a.files, lambda f: split.merge_nsp(f, out(a), j)))

    sp = add("xci2nsp", "convert XCI to NSP", lambda a, j: _each(a.files, lambda f: ops.convert_xci_to_nsp(f, out(a), j, k(a), a.split)))
    sp.add_argument("--split", action="store_true", help="split the resulting NSP for FAT32")
    add("nsp2xci", "convert NSP to XCI (games only)", lambda a, j: _each(a.files, lambda f: ops.convert_nsp_to_xci(f, out(a), j, k(a))))
    add("patch-xci", "patch XCI to run on lower firmware", lambda a, j: _each(a.files, lambda f: ops.patch_xci(f, out(a), j, k(a))))
    sp = add("patch-nsp", "patch NSP to run on lower firmware", lambda a, j: _each(a.files, lambda f: ops.patch_nsp(f, out(a), j, k(a), a.split)))
    sp.add_argument("--split", action="store_true", help="split the resulting NSP for FAT32")

    sp = add("update", "merge base game with updates/DLCs into one file",
             lambda a, j: ops.update_title(a.base, a.files, out(a), j, a.format == "xci", k(a)))
    sp.add_argument("--base", required=True, type=Path, help="base game XCI/NSP")
    sp.add_argument("--format", choices=["xci", "nsp"], default="xci")

    sp = add("compress", "NSP->NSZ / XCI->XCZ", lambda a, j: _each(a.files, lambda f: ops.compress(f, out(a), j, a.level, k(a))))
    sp.add_argument("--level", default="normal", help="fast|intermediate|normal|hard or 1-22 (default normal=17)")
    add("decompress", "NSZ->NSP / XCZ->XCI", lambda a, j: _each(a.files, lambda f: ops.decompress(f, out(a), j, k(a))))
    add("extract-fw", "extract the firmware update partition of an XCI",
        lambda a, j: _each(a.files, lambda f: ops.extract_firmware(f, out(a), j, k(a))))
    sp = add("extract-saves", "extract saves copied from the USER partition",
             lambda a, j: [ops.extract_saves(a.save_dir, out(a), j, k(a))], files=False)
    sp.add_argument("save_dir", nargs="?", type=Path, default=Path("save"))

    sp = add("extract", "extract exefs/ + romfs/ of a base game (NSP/NSZ/XCI)", lambda a, j: _each(
        a.files, lambda f: str(extract.extract_title(
            f, extract.default_dest(f, out(a), a.preset, a.lasrecomp_dir), j, k(a), a.preset, a.exefs_only).folder)))
    sp.add_argument("--preset", choices=sorted(extract.PRESETS),
                    help="lasrecomp: Link's Awakening v1.0.0 -> <out>/extracted; "
                         "totk: Tears of the Kingdom v1.0.0, ExeFS hash-checked -> <out>/totk-1.0.0")
    sp.add_argument("--lasrecomp", dest="preset", action="store_const", const="lasrecomp",
                    help="same as --preset lasrecomp")
    sp.add_argument("--totk", dest="preset", action="store_const", const="totk", help="same as --preset totk")
    sp.add_argument("--exefs-only", action="store_true", help="skip romfs/ (TOTK's is 17 GB)")
    sp.add_argument("--lasrecomp-dir", type=Path, help="LASRecomp checkout to write extracted/ into")

    sp = add("forwarder", "build an NRO / RetroArch forwarder NSP", lambda a, j: [forwarder.build(
        forwarder.ForwarderSpec(
            name=a.name, author=a.author, nro_path=a.nro, icon=a.icon, rom_path=a.rom,
            title_id=forwarder.generate_title_id() if a.title_id == "random" else a.title_id,
            version=a.version, screenshot=a.screenshot, video_capture=a.video_capture,
            no_logo=a.no_logo, custom_logo_dir=a.logo_dir,
        ), out(a), j, k(a))], files=False)
    sp.add_argument("--name", required=True)
    sp.add_argument("--author", default="AUTHOR")
    sp.add_argument("--nro", required=True, help="SD path of the .nro (RetroArch: the core .nro)")
    sp.add_argument("--rom", help="SD path of the ROM (makes a RetroArch forwarder)")
    sp.add_argument("--icon", required=True, type=Path, help="256x256 JPEG")
    sp.add_argument("--title-id", default="random", help="16 hex chars or 'random' (default)")
    sp.add_argument("--version", default="1.0")
    sp.add_argument("--screenshot", action="store_true")
    sp.add_argument("--video-capture", action="store_true")
    sp.add_argument("--no-logo", action="store_true")
    sp.add_argument("--logo-dir", type=Path, help="folder with NintendoLogo.png / StartupMovie.gif")

    add("gen-title-id", "print a random forwarder title ID", lambda a, j: print(forwarder.generate_title_id()) or [], files=False)
    add("nacp-info", "decode a control.nacp", lambda a, j: _each(a.files, lambda f: print(nacp.parse_nacp(f.read_bytes())) or []))
    add("drives", "list external disks/volumes", lambda a, j: [print(d) for d in drives.external_disks()] and [], files=False)

    sp = add("fat32", "ERASE an external disk and format it FAT32", lambda a, j: drives.format_fat32(
        a.disk, a.label, j, a.confirm, a.cluster_64k) or [], files=False)
    windows = sys.platform == "win32"  # Windows opens guiformat, which picks the drive and confirms itself
    sp.add_argument("disk", nargs="?" if windows else None, default="",
                    help="whole-disk identifier, e.g. disk4 (see `sak drives`); unused on Windows")
    sp.add_argument("--label", default="SWITCH")
    sp.add_argument("--confirm", required=not windows, default="",
                    help="repeat the disk identifier to confirm erasing it")
    sp.add_argument("--cluster-64k", action="store_true", help="64 KiB clusters (needs sudo)")

    sp = add("gui", "start the web GUI", lambda a, j: __import__("sak.gui.server", fromlist=["serve"]).serve(
        a.port, not a.no_browser, a.keys, a.out) or [], files=False)
    sp.add_argument("--port", type=int, default=0)
    sp.add_argument("--no-browser", action="store_true")

    add("doctor", "check tools and keys", cmd_doctor, files=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    job = _job(args.quiet)
    start = time.monotonic()
    try:
        result = args.fn(args, job)
    except Cancelled:
        print("\ncancelled", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        job.cancel_event.set()
        print("\ncancelled", file=sys.stderr)
        return 130
    except SakError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    if result is None:
        return 1
    if result:
        print()
        for r in result:
            print(f"-> {r}")
        print(f"Needed time {_needed_time(time.monotonic() - start)}.")
    return 0


def _needed_time(seconds: float) -> str:
    """SAK's _GETTIME format."""
    secs = round(seconds)
    if secs > 60:
        return f"{secs // 60} minutes {secs % 60} seconds"
    return f"{secs} seconds"
