"""Extract a title's ExeFS + RomFS from an NSP / NSZ / XCI (new in the macOS port).

Pipeline: container index (PFS0, or the XCI's secure HFS0) -> copy + decrypt the CNMT with
hactool -> pick the Program NCA (+ ticket title key) -> hactool --exefsdir/--romfsdir.

The LASRecomp preset (https://github.com/peiikai/LASRecomp) additionally insists on
The Legend of Zelda: Link's Awakening v1.0.0 base (no update) and writes the folder it loads:
  <dir>/extracted/exefs/{main,sdk,...}   <dir>/extracted/romfs/...
"""

from __future__ import annotations

import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import tools
from .common import Job, SakError, ensure_free_space

LA_TITLE_ID = "01006BB00C6F0000"
# First 8 bytes of exefs/main's build id that LASRecomp's launcher accepts (runtime/ui/launcher.mm).
LA_V100_BUILD_ID_PREFIX = "ae16f71e002af8cb"
META_TYPES = {0x80: "Application", 0x81: "Patch", 0x82: "AddOnContent"}
CONTENT_PROGRAM = 1


@dataclass
class Entry:
    name: str
    offset: int  # absolute offset in the container file
    size: int


@dataclass
class ContentMeta:
    title_id: str
    version: int
    meta_type: int
    programs: list[tuple[str, int]] = field(default_factory=list)  # (nca id, id offset)

    @property
    def type_name(self) -> str:
        return META_TYPES.get(self.meta_type, hex(self.meta_type))


@dataclass
class ExtractResult:
    folder: Path
    title_id: str
    version: int
    build_id: str
    exefs_files: list[str]


def _read_fs_table(fh, base: int, magic: bytes, entry_size: int) -> dict[str, Entry]:
    """Parse a PFS0 (entry 0x18) or HFS0 (entry 0x40) header located at `base`."""
    fh.seek(base)
    head = fh.read(16)
    if len(head) < 16 or head[:4] != magic:
        raise SakError(f"not a {magic.decode()} container")
    count, str_size = struct.unpack_from("<II", head, 4)
    raw = fh.read(count * entry_size)
    strings = fh.read(str_size)
    data_start = base + 16 + count * entry_size + str_size
    entries = {}
    for i in range(count):
        off, size, name_off = struct.unpack_from("<QQI", raw, i * entry_size)
        name = strings[name_off : strings.index(b"\0", name_off)].decode("utf-8", "replace")
        entries[name] = Entry(name, data_start + off, size)
    return entries


def read_container(path: Path) -> dict[str, Entry]:
    """Index an NSP/NSZ (PFS0) or the secure partition of an XCI/XCZ."""
    with open(path, "rb") as fh:
        if fh.read(4) == b"PFS0":
            return _read_fs_table(fh, 0, b"PFS0", 0x18)
        fh.seek(0x100)
        if fh.read(4) != b"HEAD":
            raise SakError(f"{Path(path).name}: neither an NSP (PFS0) nor an XCI (HEAD)")
        fh.seek(0x130)
        root_off = struct.unpack("<Q", fh.read(8))[0]
        root = _read_fs_table(fh, root_off, b"HFS0", 0x40)
        if "secure" not in root:
            raise SakError("XCI has no secure partition")
        return _read_fs_table(fh, root["secure"].offset, b"HFS0", 0x40)


def copy_entry(container: Path, entry: Entry, dest: Path, job: Job | None = None) -> Path:
    job = job or Job()
    with open(container, "rb") as src, open(dest, "wb") as out:
        src.seek(entry.offset)
        remaining = entry.size
        done = 0
        while remaining:
            job.check()
            chunk = src.read(min(1 << 22, remaining))
            if not chunk:
                raise SakError(f"{entry.name}: container is truncated")
            out.write(chunk)
            remaining -= len(chunk)
            done += len(chunk)
            job.progress(done * 100 / entry.size)
    return dest


def parse_cnmt(data: bytes) -> ContentMeta:
    title_id, version, meta_type = struct.unpack_from("<QIB", data, 0)
    ext_size, content_count = struct.unpack_from("<HH", data, 0xE)
    meta = ContentMeta(f"{title_id:016X}", version, meta_type)
    pos = 0x20 + ext_size
    for _ in range(content_count):
        nca_id = data[pos + 0x20 : pos + 0x30].hex()
        ctype, id_offset = data[pos + 0x36], data[pos + 0x37]
        if ctype == CONTENT_PROGRAM:
            meta.programs.append((nca_id, id_offset))
        pos += 0x38
    return meta


def ticket_titlekey(tik: bytes) -> tuple[str, str]:
    """Return (rights id hex, encrypted title key hex) from a common ticket."""
    sig_type = struct.unpack_from("<I", tik, 0)[0]
    sig_sizes = {0x10000: 0x200, 0x10001: 0x100, 0x10002: 0x3C, 0x10003: 0x200, 0x10004: 0x100, 0x10005: 0x3C}
    if sig_type not in sig_sizes:
        raise SakError(f"unknown ticket signature type {sig_type:#x}")
    data = 4 + sig_sizes[sig_type]
    data += (-data) % 0x40  # signature block is padded to 0x40
    if tik[data + 0x141] != 0:
        raise SakError("personalized ticket - only common (non-console-bound) tickets are supported")
    return tik[data + 0x160 : data + 0x170].hex(), tik[data + 0x40 : data + 0x50].hex()


def nso_build_id(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(0x60)
    if head[:4] != b"NSO0":
        raise SakError(f"{path.name} is not an NSO")
    return head[0x40:0x60].hex()


def _read_meta(container: Path, entries: dict[str, Entry], tmp: Path, keys: Path, job: Job) -> ContentMeta:
    cnmts = [e for n, e in entries.items() if n.endswith(".cnmt.nca")]
    if not cnmts:
        raise SakError("no .cnmt.nca in container")
    hactool = tools.find_tool("hactool")
    metas = []
    for e in cnmts:
        nca = copy_entry(container, e, tmp / e.name)
        sec = tmp / f"{e.name}.sec0"
        tools.run([str(hactool), "-k", str(keys), f"--section0dir={sec}", str(nca)], job)
        found = list(sec.glob("*.cnmt")) if sec.is_dir() else []
        if not found:
            raise SakError("could not decrypt the CNMT - are the prod.keys correct?")
        metas.append(parse_cnmt(found[0].read_bytes()))
    apps = [m for m in metas if m.meta_type == 0x80] or metas
    return apps[0]


def extract_title(src: Path, dest: Path, job: Job, keys: Path | None = None,
                  lasrecomp: bool = False) -> ExtractResult:
    """Extract exefs/ and romfs/ of the base application in `src` into `dest`."""
    src = Path(src).resolve()
    dest = Path(dest).resolve()
    if not src.is_file():
        raise SakError(f"{src} not found")
    if dest.exists() and any(dest.iterdir()):
        raise SakError(f"{dest} already exists and is not empty")
    keys = keys or tools.find_keys()
    entries = read_container(src)

    if any(n.endswith(".ncz") for n in entries):
        # NSZ/XCZ: decompress to a plain NSP/XCI first, then extract from that.
        from . import ops

        with tempfile.TemporaryDirectory(prefix="sak-unz-", dir=dest.parent) as tmp:
            job.log("compressed input - decompressing first")
            plain = ops.decompress(src, Path(tmp), job, keys)[0]
            return extract_title(plain, dest, job, keys, lasrecomp)

    dest.parent.mkdir(parents=True, exist_ok=True)
    hactool = tools.find_tool("hactool")
    with tools.sanitized_keys(keys) as clean, \
            tempfile.TemporaryDirectory(prefix="sak-extract-", dir=dest.parent) as tmpdir:
        tmp = Path(tmpdir)
        meta = _read_meta(src, entries, tmp, clean, job)
        job.log(f"title {meta.title_id} v{meta.version} ({meta.type_name})")
        if meta.meta_type != 0x80:
            raise SakError(f"{meta.type_name} content can't be extracted on its own - use the base game")
        if lasrecomp:
            if meta.title_id != LA_TITLE_ID:
                raise SakError(f"LASRecomp needs Link's Awakening ({LA_TITLE_ID}), this is {meta.title_id}")
            if meta.version != 0:
                raise SakError(f"LASRecomp needs v1.0.0 without updates, this is v{meta.version}")
        programs = sorted(meta.programs, key=lambda p: p[1])
        if not programs:
            raise SakError("no Program NCA listed in the CNMT")
        nca_name = programs[0][0] + ".nca"
        if nca_name not in entries:
            raise SakError(f"Program NCA {nca_name} missing from the container")
        program = entries[nca_name]
        ensure_free_space(dest.parent, program.size * 2, "Extraction")

        cmd = [str(hactool), "-k", str(clean)]
        tid = meta.title_id.lower()
        for name, e in entries.items():
            if name.endswith(".tik") and name.lower().startswith(tid):  # rights id = title id + key gen
                with open(src, "rb") as fh:
                    fh.seek(e.offset)
                    _, titlekey = ticket_titlekey(fh.read(e.size))
                cmd.append(f"--titlekey={titlekey}")
                job.log(f"using ticket {name}")
                break

        job.log(f"copying Program NCA {nca_name} ({program.size} bytes)")
        nca = copy_entry(src, program, tmp / nca_name, job)
        work = tmp / "out"
        work.mkdir()  # hactool creates the exefs/romfs dirs but not their parent
        cmd += [f"--exefsdir={work / 'exefs'}", f"--romfsdir={work / 'romfs'}", str(nca)]
        job.log("extracting exefs + romfs (this takes a while for big games)")
        code, _ = tools.run(cmd, job)
        nca.unlink()
        main = work / "exefs" / "main"
        if not main.is_file():
            raise SakError(f"hactool extracted no exefs/main (exit {code}) - are the prod.keys correct?")
        if lasrecomp and not (work / "exefs" / "sdk").is_file():
            raise SakError("exefs/sdk missing - LASRecomp needs both main and sdk")
        build_id = nso_build_id(main)
        if lasrecomp and not build_id.startswith(LA_V100_BUILD_ID_PREFIX):
            raise SakError(f"exefs/main build id {build_id[:16]} is not Link's Awakening v1.0.0 "
                           f"({LA_V100_BUILD_ID_PREFIX}) - LASRecomp will reject it")
        exefs_files = sorted(p.name for p in (work / "exefs").iterdir())
        if dest.exists():
            dest.rmdir()
        shutil.move(str(work), dest)

    job.progress(100)
    job.log(f"extracted to {dest}\nexefs: {', '.join(exefs_files)}\nmain build id: {build_id}")
    return ExtractResult(dest, meta.title_id, meta.version, build_id, exefs_files)


def default_dest(src: Path, out_dir: Path, lasrecomp: bool, lasrecomp_dir: Path | None = None) -> Path:
    if lasrecomp:
        return Path(lasrecomp_dir or out_dir) / "extracted"
    return Path(out_dir) / Path(src).stem
