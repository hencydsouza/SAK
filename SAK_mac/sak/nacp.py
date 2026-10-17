"""Minimal NACP (control.nacp) writer replacing hptnacp.exe.

Produces the same values SAK's generated control.xml asked hptnacp to encode.
Layout per switchbrew "NACP Format"; offsets cross-checked with NSC_Builder's Fs/Nacp.py parser.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

NACP_SIZE = 0x4000

LANGUAGES = [
    "AmericanEnglish", "BritishEnglish", "Japanese", "French", "German",
    "LatinAmericanSpanish", "Spanish", "Italian", "Dutch", "CanadianFrench",
    "Portuguese", "Russian", "Korean", "TraditionalChinese", "SimplifiedChinese",
    "BrazilianPortuguese",
]
RATING_ORGS = [
    "CERO", "GRACGCRB", "GSRMR", "ESRB", "ClassInd", "USK", "PEGI", "PEGIPortugal",
    "PEGIBBFC", "Russian", "ACB", "OFLC", "IARCGeneric",
]

# SAK writes <Title> entries for these 12 languages ...
SAK_TITLE_LANGUAGES = LANGUAGES[:12]
# ... but lists Portuguese only as a title, not as a <SupportedLanguage>.
SAK_SUPPORTED_LANGUAGES = [lang for lang in LANGUAGES[:12] if lang != "Portuguese"]
SAK_RATINGS = {
    "CERO": 12, "ESRB": 10, "USK": 12, "PEGI": 12, "PEGIPortugal": 12,
    "PEGIBBFC": 12, "Russian": 12, "ACB": 13, "OFLC": 13,
}
HBL_ID = 0x0104444444441001
HBL_AOC_BASE_ID = 0x0104444444442001

LOGO_TYPE_NINTENDO = 2
VIDEO_CAPTURE_DISABLE, VIDEO_CAPTURE_ENABLE = 0, 2
SCREENSHOT_ALLOW, SCREENSHOT_DENY = 0, 1
DATA_LOSS_CONFIRMATION_REQUIRED = 1


@dataclass
class NacpConfig:
    name: str = "hbl"
    publisher: str = "Unspecified Author"
    display_version: str = "1.0"
    screenshot: bool = False
    video_capture: bool = False
    title_languages: list[str] = field(default_factory=lambda: list(SAK_TITLE_LANGUAGES))
    supported_languages: list[str] = field(default_factory=lambda: list(SAK_SUPPORTED_LANGUAGES))
    ratings: dict[str, int] = field(default_factory=lambda: dict(SAK_RATINGS))
    presence_group_id: int = HBL_ID
    save_data_owner_id: int = HBL_ID
    local_communication_id: int = HBL_ID
    add_on_content_base_id: int = HBL_AOC_BASE_ID


def _put_str(buf: bytearray, offset: int, size: int, text: str) -> None:
    raw = text.encode("utf-8")
    if len(raw) >= size:
        # keep a NUL terminator and never cut a UTF-8 sequence in half
        raw = raw[: size - 1].decode("utf-8", "ignore").encode("utf-8")
    buf[offset : offset + len(raw)] = raw


def build_nacp(cfg: NacpConfig) -> bytes:
    buf = bytearray(NACP_SIZE)
    for lang in cfg.title_languages:
        base = LANGUAGES.index(lang) * 0x300
        _put_str(buf, base, 0x200, cfg.name)
        _put_str(buf, base + 0x200, 0x100, cfg.publisher)

    # 0x3000 Isbn (empty), 0x3025 StartupUserAccount=None, 0x3026 UserAccountSwitchLock=Disable,
    # 0x3027 AddOnContentRegistrationType=AllOnLaunch, 0x3028 Attribute=None: all zero.
    supported = 0
    for lang in cfg.supported_languages:
        supported |= 1 << LANGUAGES.index(lang)
    struct.pack_into("<I", buf, 0x302C, supported)
    # 0x3030 ParentalControl=None (0)
    buf[0x3034] = SCREENSHOT_ALLOW if cfg.screenshot else SCREENSHOT_DENY
    buf[0x3035] = VIDEO_CAPTURE_ENABLE if cfg.video_capture else VIDEO_CAPTURE_DISABLE
    buf[0x3036] = DATA_LOSS_CONFIRMATION_REQUIRED
    # 0x3037 PlayLogPolicy=All (0)
    struct.pack_into("<Q", buf, 0x3038, cfg.presence_group_id)
    for i, org in enumerate(RATING_ORGS):
        buf[0x3040 + i] = cfg.ratings.get(org, -1) & 0xFF
    for i in range(len(RATING_ORGS), 0x20):
        buf[0x3040 + i] = 0xFF
    _put_str(buf, 0x3060, 0x10, cfg.display_version)
    struct.pack_into("<Q", buf, 0x3070, cfg.add_on_content_base_id)
    struct.pack_into("<Q", buf, 0x3078, cfg.save_data_owner_id)
    # 0x3080..0x30A8 save sizes / BCAT size: 0. 0x30A8 ApplicationErrorCodeCategory: empty.
    for i in range(8):
        struct.pack_into("<Q", buf, 0x30B0 + i * 8, cfg.local_communication_id)
    buf[0x30F0] = LOGO_TYPE_NINTENDO
    # 0x30F1 LogoHandling=Auto, 0x30F2 RuntimeAddOnContentInstall=Deny, 0x30F6 CrashReport=Deny,
    # 0x30F7 Hdcp=None, 0x30F8 SeedForPseudoDeviceId=0, 0x3100 BcatPassphrase empty,
    # 0x3148.. save size maxima, temporary/cache storage: 0; 0x3190 PlayLogQueryable ids: 0;
    # 0x3210 PlayLogQueryCapability=None, 0x3211 Repair=None, 0x3212 ProgramIndex=0,
    # 0x3213 RequiredNetworkServiceLicenseOnLaunch=None: all zero.
    return bytes(buf)


def parse_nacp(data: bytes) -> dict:
    """Decode the fields build_nacp writes (used by tests and `sak nacp-info`)."""
    if len(data) != NACP_SIZE:
        raise ValueError(f"NACP must be {NACP_SIZE:#x} bytes, got {len(data):#x}")

    def cstr(off: int, size: int) -> str:
        return data[off : off + size].split(b"\0", 1)[0].decode("utf-8", "replace")

    titles = {}
    for i, lang in enumerate(LANGUAGES):
        name = cstr(i * 0x300, 0x200)
        if name:
            titles[lang] = (name, cstr(i * 0x300 + 0x200, 0x100))
    supported = struct.unpack_from("<I", data, 0x302C)[0]
    return {
        "titles": titles,
        "supported_languages": [l for i, l in enumerate(LANGUAGES) if supported >> i & 1],
        "screenshot": data[0x3034],
        "video_capture": data[0x3035],
        "data_loss_confirmation": data[0x3036],
        "presence_group_id": struct.unpack_from("<Q", data, 0x3038)[0],
        "ratings": {o: data[0x3040 + i] for i, o in enumerate(RATING_ORGS) if data[0x3040 + i] != 0xFF},
        "display_version": cstr(0x3060, 0x10),
        "add_on_content_base_id": struct.unpack_from("<Q", data, 0x3070)[0],
        "save_data_owner_id": struct.unpack_from("<Q", data, 0x3078)[0],
        "local_communication_id": struct.unpack_from("<Q", data, 0x30B0)[0],
        "logo_type": data[0x30F0],
    }
