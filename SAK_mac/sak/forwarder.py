"""NRO / RetroArch forwarder NSP builder (port of SAK's _NROFORWARDER).

Layout handed to hacBrewPack:
  control/control.nacp, control/icon_AmericanEnglish.dat (256x256 JPEG)
  exefs/main, exefs/main.npdm            (nx-hbloader forwarder build shipped with SAK)
  logo/NintendoLogo.png, logo/StartupMovie.gif
  romfs/nextNroPath  -> "sdmc:<nro>"
  romfs/nextArgv     -> "sdmc:<nro>"  or  'sdmc:<core> "sdmc:<rom>"' for RetroArch
"""

from __future__ import annotations

import random
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import nacp, tools
from .common import Job, SakError

_TITLE_ID_ALPHABET = "ABCDEF1234567890"
DEFAULT_TITLE_ID = "05D37A1090380000"


def generate_title_id(rng: random.Random | None = None) -> str:
    """SAK's GENERATETITLEID: "01" + 11 random hex chars + "000"."""
    rng = rng or random.SystemRandom()
    return "01" + "".join(rng.choice(_TITLE_ID_ALPHABET) for _ in range(11)) + "000"


@dataclass
class ForwarderSpec:
    name: str
    author: str
    nro_path: str  # path on the SD card, e.g. /switch/App-Dir/App.nro (or RetroArch core .nro)
    icon: Path  # 256x256 JPEG
    title_id: str = DEFAULT_TITLE_ID
    version: str = "1.0"
    rom_path: str | None = None  # set for a RetroArch forwarder
    screenshot: bool = False
    video_capture: bool = False
    no_logo: bool = False
    custom_logo_dir: Path | None = None  # may contain NintendoLogo.png / StartupMovie.gif


def jpeg_size(path: Path) -> tuple[int, int] | None:
    """Return (width, height) of a baseline/progressive JPEG, or None if not a JPEG."""
    data = Path(path).read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            return w, h
        i += 2 + seg_len
    return None


def _sd_path(p: str) -> str:
    p = p.strip()
    if p.startswith("sdmc:"):
        p = p[5:]
    if not p.startswith("/"):
        p = "/" + p
    return "sdmc:" + p


def validate(spec: ForwarderSpec) -> None:
    if len(spec.title_id) != 16:
        raise SakError("NRO forwarder failed, TitleID must be 16 chars!")
    if not re.fullmatch(r"[0-9A-Fa-f]{16}", spec.title_id):
        raise SakError("NRO forwarder failed, TitleID must be hexadecimal!")
    if not spec.name.strip():
        raise SakError("NRO forwarder failed, title name is empty")
    icon = Path(spec.icon)
    if not icon.is_file():
        raise SakError(f"NRO forwarder failed, icon {icon} not found")
    size = jpeg_size(icon)
    if size is None:
        raise SakError("NRO forwarder failed, icon must be a JPEG (.jpg)")
    if size != (256, 256):
        raise SakError(f"NRO forwarder failed, icon must be 256x256 (got {size[0]}x{size[1]})")


def stage(spec: ForwarderSpec, root: Path) -> None:
    """Create the hacBrewPack input tree under root."""
    validate(spec)
    for d in ("control", "exefs", "logo", "romfs"):
        (root / d).mkdir(parents=True, exist_ok=True)

    cfg = nacp.NacpConfig(
        name=spec.name, publisher=spec.author, display_version=spec.version,
        screenshot=spec.screenshot, video_capture=spec.video_capture,
    )
    (root / "control" / "control.nacp").write_bytes(nacp.build_nacp(cfg))
    shutil.copyfile(spec.icon, root / "control" / "icon_AmericanEnglish.dat")
    shutil.copyfile(tools.resource("nro_fw_main"), root / "exefs" / "main")
    shutil.copyfile(tools.resource("nro_fw_main.npdm"), root / "exefs" / "main.npdm")

    # SAK picked up a "custom" folder next to SAK.exe automatically
    logo_dir = spec.custom_logo_dir or (tools.PORT_ROOT / "custom")
    for logo, res in (("NintendoLogo.png", "nro_fw_NintendoLogo.png"), ("StartupMovie.gif", "nro_fw_StartupMovie.gif")):
        custom = Path(logo_dir) / logo
        src = custom if custom.is_file() else tools.resource(res)
        shutil.copyfile(src, root / "logo" / logo)

    nro = _sd_path(spec.nro_path)
    argv = nro if not spec.rom_path else f'{nro} "{_sd_path(spec.rom_path)}"'
    # SAK wrote these with FileWrite: no trailing newline.
    (root / "romfs" / "nextArgv").write_text(argv, encoding="utf-8")
    (root / "romfs" / "nextNroPath").write_text(nro, encoding="utf-8")


def build(spec: ForwarderSpec, out_dir: Path, job: Job, keys: Path | None = None) -> Path:
    keys = keys or tools.find_keys()
    hacbrewpack = tools.find_tool("hacbrewpack")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sak-nrofwd-", dir=out_dir) as tmp:
        root = Path(tmp)
        stage(spec, root)
        cmd = [
            str(hacbrewpack), "-k", str(keys),
            "--nspdir", str(root / "hcb_nsp"), "--ncadir", str(root / "hcb_nca"),
            "--tempdir", str(root / "hcb_tmp"), "--backupdir", str(root / "hcb_back"),
            "--controldir", str(root / "control"), "--exefsdir", str(root / "exefs"),
            "--logodir", str(root / "logo"), "--romfsdir", str(root / "romfs"),
            "--titleid", spec.title_id, "--titlename", spec.name, "--titlepublisher", spec.author,
        ]
        if spec.no_logo:
            cmd.append("--nologo")
        with tools.sanitized_keys(keys) as clean:
            cmd[2] = str(clean)
            code, _ = tools.run(cmd, job, cwd=root)
        built = sorted((root / "hcb_nsp").glob("*.nsp"))
        if not built:
            raise SakError(f"NRO forwarder create failed: {spec.name} (hacbrewpack exit {code})")
        safe = re.sub(r'[\\/:*?"<>|]', "_", spec.name)  # also invalid in Windows file names
        dest = out_dir / f"{safe}-{built[0].name}"
        shutil.copyfile(built[0], dest)
    job.log(f"NRO forwarder created: {dest}")
    return dest
