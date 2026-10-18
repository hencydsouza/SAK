"""Tool-driven operations (ports of SAK's _CONVERT*/_PATCH*/_UPDATEXCI/_*COMPRESS*/_EXTRACT*).

The dead Windows-only helpers are replaced:
  4nxci.exe (XCI->NSP)      -> NSC_Builder squirrel `-t nsp -dc`
  renxpack.exe (patch NSP)  -> NSC_Builder squirrel `-t nsp -dc -pv true -kp 0` (same as SAK's XCI patch)
  hactoolnet.exe (saves)    -> savex (small LibHac-based tool, see savex/)
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import split, tools
from .common import Job, SakError, ensure_free_space

_MIN_CHECKED_INPUT = 100 * 1024 * 1024

COMPRESSION_LEVELS = {"fast": 1, "intermediate": 10, "normal": 17, "hard": 22}

# squirrel flag sets exactly as SAK v0.7.14 passed them
_SQ_COMMON = ["-b", "65536", "-pv", "false", "-kp", "false", "--RSVcap", "268435656",
              "-fat", "exfat", "-fx", "files", "-ND", "true"]
_SQ_PATCH = ["-b", "65536", "-pv", "true", "-kp", "0", "--RSVcap", "0",
             "-fat", "exfat", "-fx", "files", "-ND", "true"]


def _total_size(paths) -> int:
    missing = [str(p) for p in paths if not Path(p).is_file()]
    if missing:
        raise SakError("not found: " + ", ".join(missing))
    return sum(Path(p).stat().st_size for p in paths)


def _squirrel(args: list[str], job: Job, keys: Path, out_dir: Path, patterns: list[str],
              list_files: list[Path] | None = None) -> list[Path]:
    """Run NSC_Builder squirrel.py into a scratch output dir, then move results to out_dir.

    squirrel chdirs into its own ztools folder, so every path must be absolute, and keys are
    handed over via NSCB_KEYS_FILE (honoured by scripts/patches/nscb-macos.patch).
    Its exit code is unreliable (errors often exit 0), so success = an output file appeared.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in list_files or []:
        if not f.is_file():
            raise SakError(f"{f} not found")
    with tempfile.TemporaryDirectory(prefix="sak-squirrel-", dir=out_dir) as tmp:
        sq_out = Path(tmp) / "squirrel"
        sq_out.mkdir()
        argv = list(args)
        if list_files is not None:
            (sq_out / "list.txt").write_text("".join(f"{p}\n" for p in list_files), encoding="utf-8")
            argv += ["-tfile", str(sq_out / "list.txt")]
        argv += ["-o", str(sq_out)]
        code, _ = tools.run(tools.squirrel_cmd() + argv, job, cwd=Path(tmp),
                            env={"NSCB_KEYS_FILE": str(Path(keys).resolve())})
        produced = sorted({p for pat in patterns for p in sq_out.glob(pat)
                           if p.is_file() and p.stat().st_size > 0 and not p.stem.startswith("UNKNOWN")})
        if not produced:
            raise SakError(f"squirrel produced no output (exit {code}).\nAre the keys.dat/prod.keys correct?")
        # Guard against silently truncated output (e.g. the upstream bug that unlinked the
        # NSP mid-write, leaving a few KB "success" for a multi-GB job).
        inputs = list_files or [Path(args[args.index("-dc") + 1])]
        size_in = sum(Path(p).stat().st_size for p in inputs)
        size_out = sum(p.stat().st_size for p in produced)
        if size_in > _MIN_CHECKED_INPUT and size_out < size_in / 1000:
            raise SakError(f"squirrel output looks truncated ({size_out} bytes from {size_in} bytes of input)")
        moved = []
        for p in produced:
            dest = out_dir / p.name
            shutil.move(str(p), dest)
            moved.append(dest)
        return moved


def _lowercase_ext(path: Path, tmp: Path) -> Path:
    """squirrel's compress/decompress only recognise lower-case extensions."""
    path = Path(path).resolve()
    if path.suffix == path.suffix.lower():
        return path
    if sys.platform == "win32":
        # NTFS/exFAT names are case-insensitive, and symlinks need admin rights: same file, new spelling.
        return path.with_suffix(path.suffix.lower())
    link = tmp / (path.stem + path.suffix.lower())
    link.symlink_to(path)
    return link


def _maybe_split_nsp(files: list[Path], out_dir: Path, job: Job, split_after: bool) -> list[Path]:
    if not split_after:
        return files
    result = []
    for f in files:
        if f.suffix.lower() == ".nsp" and f.stat().st_size >= split.SPLIT_BYTES:
            result.append(split.split_nsp(f, out_dir, job))
        else:
            result.append(f)
    return result


def convert_xci_to_nsp(xci: Path, out_dir: Path, job: Job, keys: Path | None = None,
                       split_after: bool = False) -> list[Path]:
    keys = keys or tools.find_keys()
    ensure_free_space(Path(out_dir), _total_size([xci]), f"Converting {Path(xci).name}")
    files = _squirrel(_SQ_COMMON + ["-t", "nsp", "-dc", str(Path(xci).resolve())], job, keys, out_dir, ["*.nsp"])
    return _maybe_split_nsp(files, Path(out_dir), job, split_after)


def convert_nsp_to_xci(nsp: Path, out_dir: Path, job: Job, keys: Path | None = None) -> list[Path]:
    keys = keys or tools.find_keys()
    ensure_free_space(Path(out_dir), _total_size([nsp]), f"Converting {Path(nsp).name}")
    return _squirrel(_SQ_COMMON + ["-t", "xci", "-dc", str(Path(nsp).resolve())], job, keys, out_dir, ["*.xci"])


def patch_xci(path: Path, out_dir: Path, job: Job, keys: Path | None = None) -> list[Path]:
    """Lower required firmware / master key; output named *_patched.xci like SAK."""
    keys = keys or tools.find_keys()
    ensure_free_space(Path(out_dir), _total_size([path]), f"Patching {Path(path).name}")
    files = _squirrel(_SQ_PATCH + ["-t", "xci", "-dc", str(Path(path).resolve())], job, keys, out_dir, ["*.xci"])
    return [_rename_patched(f) for f in files]


def patch_nsp(path: Path, out_dir: Path, job: Job, keys: Path | None = None,
              split_after: bool = False) -> list[Path]:
    keys = keys or tools.find_keys()
    ensure_free_space(Path(out_dir), _total_size([path]), f"Patching {Path(path).name}")
    files = _squirrel(_SQ_PATCH + ["-t", "nsp", "-dc", str(Path(path).resolve())], job, keys, out_dir, ["*.nsp"])
    files = [_rename_patched(f) for f in files]
    return _maybe_split_nsp(files, Path(out_dir), job, split_after)


def _rename_patched(f: Path) -> Path:
    dest = f.with_name(f"{f.stem}_patched{f.suffix}")
    f.replace(dest)
    return dest


def update_title(base: Path, updates: list[Path], out_dir: Path, job: Job, as_xci: bool,
                 keys: Path | None = None) -> list[Path]:
    """Merge base game + updates/DLCs into one XCI or NSP (SAK 'Update XCI/NSP')."""
    keys = keys or tools.find_keys()
    inputs = [Path(p).resolve() for p in [base, *updates]]
    ensure_free_space(Path(out_dir), _total_size(inputs), "Updating")
    kind = "xci" if as_xci else "nsp"
    args = _SQ_COMMON + ["-t", kind, "-roma", "TRUE", "-dmul", "calculate"]
    return _squirrel(args, job, keys, out_dir, [f"*.{kind}"], list_files=inputs)


def compress(path: Path, out_dir: Path, job: Job, level: str | int = "normal",
             keys: Path | None = None) -> list[Path]:
    """NSP->NSZ or XCI->XCZ."""
    keys = keys or tools.find_keys()
    lvl = COMPRESSION_LEVELS.get(str(level).lower(), level)
    if not str(lvl).isdigit() or not 1 <= int(lvl) <= 22:
        raise SakError(f"invalid compression level {level!r}")
    ensure_free_space(Path(out_dir), _total_size([path]), f"Compressing {Path(path).name}")
    args = ["-b", "65536", "--compress", str(lvl), "--threads", "0", "--nodelta", "-ND true", "--fexport", "xcz"]
    with tempfile.TemporaryDirectory(prefix="sak-in-") as tmp:
        src = _lowercase_ext(path, Path(tmp))
        return _squirrel(args, job, keys, out_dir, ["*.nsz", "*.xcz"], list_files=[src])


def decompress(path: Path, out_dir: Path, job: Job, keys: Path | None = None) -> list[Path]:
    """NSZ->NSP or XCZ->XCI."""
    keys = keys or tools.find_keys()
    ensure_free_space(Path(out_dir), _total_size([path]) * 2, f"Decompressing {Path(path).name}")
    args = ["-b", "65536", "--decompress", "auto"]
    with tempfile.TemporaryDirectory(prefix="sak-in-") as tmp:
        src = _lowercase_ext(path, Path(tmp))
        return _squirrel(args, job, keys, out_dir, ["*.nsp", "*.xci"], list_files=[src])


def firmware_version(nca_names: list[str], mapping_csv: Path | None = None) -> str:
    """Match extracted NCA names against fw_nca_mapping.csv (`version;nca_id`)."""
    mapping_csv = mapping_csv or tools.resource("fw_nca_mapping.csv")
    with open(mapping_csv, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh, delimiter=";") if len(r) >= 2]
    for nca in nca_names:
        for version, nca_id in rows:
            if nca_id.strip() and nca_id.strip() in nca:
                return version.strip()
    return "Unknown"


def extract_firmware(xci: Path, out_dir: Path, job: Job, keys: Path | None = None) -> Path:
    """Dump the XCI's update partition (firmware) to 'Firmware <version>'."""
    keys = keys or tools.find_keys()
    hactool = tools.find_tool("hactool")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_free_space(out_dir, _total_size([xci]), f"Extracting firmware from {Path(xci).name}")
    tmp = out_dir / "tmpout"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with tools.sanitized_keys(keys) as clean:
            tools.run([str(hactool), "-x", "-r", "-y", "-k", str(clean), "-t", "xci",
                       f"--updatedir={tmp}", str(xci)], job, cwd=out_dir)
        if not tmp.is_dir() or not any(tmp.iterdir()):
            raise SakError("hactool extracted no firmware files.\nAre the prod.keys correct?")
        version = firmware_version([p.name for p in tmp.iterdir()])
        dest = out_dir / f"Firmware {version}"
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    job.log(f"Firmware {version} extracted to {dest}")
    return dest


def extract_saves(save_dir: Path, out_dir: Path, job: Job, keys: Path | None = None) -> Path:
    """Extract every save file in `save_dir` (copied from the console's USER/save folder).

    Output: extracted_save_<date>/<UserID>/<TitleID>/<Timestamp> <SaveID>/...
    """
    keys = keys or tools.find_keys()
    savex = tools.find_tool("savex")
    save_dir = Path(save_dir)
    if not save_dir.is_dir():
        raise SakError(f"No save folder found :-(\nCopy your save folder from your USER partition to {save_dir}")
    saves = sorted(p for p in save_dir.iterdir() if p.is_file())
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    root = Path(out_dir) / f"extracted_save_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    for i, save in enumerate(saves):
        job.check()
        job.progress(i * 100 / len(saves))
        with tempfile.TemporaryDirectory(prefix="sak-save-", dir=root) as tmp:
            code, output = tools.run([str(savex), "-k", str(keys), "--outdir", tmp, str(save)], job)
            meta = _parse_save_meta(output)
            if code != 0 or not meta.get("save_type"):
                job.log(f"skipped {save.name} (not a readable save, exit {code})")
                continue
            ts = re.sub(r"[:/]", "-", meta.get("timestamp", ""))
            dest = root / meta["user_id"] / meta["title_id"] / f"{ts} {meta['save_id']}".strip()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(tmp, dest)
            os.makedirs(tmp, exist_ok=True)  # TemporaryDirectory cleanup expects it
            extracted += 1
    job.progress(100)
    job.log(f"Save extraction finished: {extracted}/{len(saves)} saves -> {root}")
    return root


def _parse_save_meta(output: str) -> dict:
    for line in reversed(output.splitlines()):
        if line.startswith("JSON:"):
            try:
                return json.loads(line[5:])
            except json.JSONDecodeError:
                break
    # fall back to hactoolnet-style "Key: value" lines
    keys = {"Title ID": "title_id", "User ID": "user_id", "Save ID": "save_id",
            "Save Type": "save_type", "Timestamp": "timestamp"}
    meta = {}
    for line in output.splitlines():
        name, _, value = line.strip().partition(":")
        if name.strip() in keys:
            meta[keys[name.strip()]] = value.strip()
    return meta
