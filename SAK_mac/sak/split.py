"""Split / merge XCI and NSP files for FAT32 (port of _SPLITXCI/_MERGEXCI/_SPLITNSP/_MERGENSP)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import (
    READ_BYTES,
    SPLIT_BYTES,
    Job,
    SakError,
    copy_stream,
    ensure_free_space,
    human_size,
    ireplace,
)

CHUNKS_PER_PART = SPLIT_BYTES // READ_BYTES
# SAK's trim check is StringInStr(binary, "0xFFFFFFFFFFFFFFFFFFFF"): AutoIt renders a binary
# chunk as "0x<HEX>", so this only matches chunks that *start* with 10 bytes of 0xFF padding.
_TRIM_PREFIX = b"\xff" * 10


def set_archive_bit(path: Path, job: Job | None = None) -> bool:
    """Best effort: mark a folder as a Horizon "concatenation file" (FAT archive bit).

    macOS msdosfs maps the BSD SF_ARCHIVED flag inversely to ATTR_ARCHIVE, so `chflags noarch`
    is what sets the FAT archive attribute. Only meaningful on a FAT32/exFAT volume; on
    APFS this is a no-op for the Switch and must be redone after copying (e.g. Hekate
    "Fix Archive bit").
    """
    result = subprocess.run(["chflags", "noarch", str(path)], capture_output=True, text=True)
    if result.returncode != 0 and job:
        job.log(f"warning: could not set archive bit on {path}: {result.stderr.strip()}")
    return result.returncode == 0


def _split_parts(src: Path, part_path, job: Job, trim: bool, parts: list[Path]) -> list[Path]:
    """Write parts, appending each to `parts` as it is created (so callers can clean up)."""
    total = src.stat().st_size
    done = 0
    with src.open("rb") as fin:
        index = 0
        while done < total:
            out = part_path(index)
            parts.append(out)
            with out.open("wb") as fout:
                for _ in range(CHUNKS_PER_PART):
                    job.check()
                    chunk = fin.read(READ_BYTES)
                    if not chunk:
                        break
                    # SAK only trims parts after the first one.
                    if not (trim and index > 0 and chunk.startswith(_TRIM_PREFIX)):
                        fout.write(chunk)
                    done += len(chunk)
                    job.progress(done * 100 / total)
            index += 1
    return parts


def _check_splittable(src: Path, out_dir: Path) -> int:
    if not src.is_file():
        raise SakError(f"{src} not found")
    size = src.stat().st_size
    ensure_free_space(out_dir, size, f"Splitting for {src.name}")
    if size < SPLIT_BYTES:
        raise SakError(f"Splitting for {src.name} aborted\nFileSize is lower than 4GB - Size is: {human_size(size)}")
    return size


def split_xci(src: Path, out_dir: Path, job: Job, trim: bool = False) -> list[Path]:
    """Name.xci -> Name_splitted.xc0, .xc1, ... (spaces become underscores)."""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _check_splittable(src, out_dir)
    base = ireplace(src.name.replace(" ", "_"), ".xci", "_splitted.xc")
    parts: list[Path] = []
    try:
        _split_parts(src, lambda i: out_dir / f"{base}{i}", job, trim, parts)
    except BaseException:
        for p in parts:
            p.unlink(missing_ok=True)
        raise
    job.log(f"Splitting for {src.name} finished\nSplitted up to {len(parts)} parts")
    return parts


def split_nsp(src: Path, out_dir: Path, job: Job) -> Path:
    """Name.nsp -> folder Name_splitted.nsp/ with parts 00, 01, ... and the archive bit set."""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _check_splittable(src, out_dir)
    folder = out_dir / ireplace(src.name.replace(" ", "_"), ".nsp", "_splitted.nsp")
    if folder.exists():
        _rmtree(folder)
    folder.mkdir()
    set_archive_bit(folder, job)
    try:
        parts = _split_parts(src, lambda i: folder / f"{i:02d}", job, False, [])
    except BaseException:
        _rmtree(folder)
        raise
    job.log(f"Splitting for {src.name} finished\nSplitted up to {len(parts)} parts")
    return folder


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def find_xci_parts(first: Path) -> tuple[list[Path], str]:
    """Return (ordered parts, display name) for any SAK-supported split XCI naming."""
    first = Path(first)
    name = first.name
    lower = name.lower()
    if lower.endswith(".xc0"):
        stem = name[:-1]
        nxt = lambda i: first.with_name(f"{stem}{i}")  # noqa: E731
    elif lower.endswith(".xci.00"):
        stem = name[:-2]
        nxt = lambda i: first.with_name(f"{stem}{i:02d}")  # noqa: E731
    elif name == "00":
        # Folder-style split ("Game.xci/00, 01, ..."). SAK looked up "01"... relative to its own
        # working directory (a bug); we look next to the first part.
        name = first.parent.name
        nxt = lambda i: first.with_name(f"{i:02d}")  # noqa: E731
    else:
        raise SakError(f"{first.name}: expected a .xc0, .xci.00 or 00 first part")
    parts = [first]
    i = 1
    while nxt(i).exists():
        parts.append(nxt(i))
        i += 1
    return parts, name


def merged_xci_name(name: str) -> str:
    out = ireplace(name, ".xc0", "_merged.xci")
    out = ireplace(out, ".xci.00", "_merged.xci")
    out = ireplace(out, ".xci", "_merged.xci")
    return ireplace(out, "_merged_merged", "_merged")


def merge_xci(first: Path, out_dir: Path, job: Job) -> Path:
    parts, name = find_xci_parts(first)
    return _merge(parts, Path(out_dir) / merged_xci_name(name), job)


def find_nsp_parts(src: Path) -> tuple[list[Path], str]:
    """Accept a split-NSP folder or its 00 part."""
    src = Path(src)
    folder = src if src.is_dir() else src.parent
    first = folder / "00"
    if not first.is_file():
        raise SakError(f"{folder}: no 00 part found")
    parts = [first]
    i = 1
    while (folder / f"{i:02d}").is_file():
        parts.append(folder / f"{i:02d}")
        i += 1
    return parts, folder.name


def merged_nsp_name(name: str) -> str:
    out = ireplace(name, ".nsp", "_merged.nsp")
    out = ireplace(out, "splitted_", "")
    if ".nsp" not in out.lower():
        out += "_merged.nsp"
    return out


def merge_nsp(src: Path, out_dir: Path, job: Job) -> Path:
    parts, name = find_nsp_parts(src)
    return _merge(parts, Path(out_dir) / merged_nsp_name(name), job)


def _merge(parts: list[Path], dest: Path, job: Job) -> Path:
    if len(parts) < 2:
        raise SakError(f"{parts[0].name}: only one part found, nothing to merge")
    total = sum(p.stat().st_size for p in parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ensure_free_space(dest.parent, total, f"Merging for {dest.name}")
    dest.unlink(missing_ok=True)
    done = 0
    try:
        with dest.open("wb") as fout:
            for part in parts:
                with part.open("rb") as fin:
                    done = copy_stream(fin, fout, total, done, job)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    job.log(f"Merging finished: {dest.name} ({len(parts)} parts)")
    return dest
