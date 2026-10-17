import http.client
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sak import extract, forwarder, nacp, ops, split, tools  # noqa: E402
from sak.common import Job, SakError  # noqa: E402

PART = 4 * 32768  # shrink the 0xFFFF0000 split size to 4 blocks for tests


def small_split():
    return mock.patch.multiple(split, SPLIT_BYTES=PART, CHUNKS_PER_PART=PART // 32768)


def fake_jpeg(w=256, h=256) -> bytes:
    sof = b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"


def write_exe(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()


class SplitMergeTests(Tmp):
    def test_xci_roundtrip(self):
        data = os.urandom(PART * 2 + 1000)
        src = self.tmp / "My Game.xci"
        src.write_bytes(data)
        with small_split():
            parts = split.split_xci(src, self.tmp / "out", Job())
        self.assertEqual([p.name for p in parts], ["My_Game_splitted.xc0", "My_Game_splitted.xc1", "My_Game_splitted.xc2"])
        self.assertEqual(parts[0].stat().st_size, PART)
        merged = split.merge_xci(parts[0], self.tmp / "m", Job())
        self.assertEqual(merged.name, "My_Game_splitted_merged.xci")
        self.assertEqual(merged.read_bytes(), data)

    def test_exact_multiple_has_no_empty_tail_part(self):
        src = self.tmp / "g.xci"
        src.write_bytes(os.urandom(PART * 2))
        with small_split():
            parts = split.split_xci(src, self.tmp, Job())
        self.assertEqual(len(parts), 2)

    def test_too_small_refused(self):
        src = self.tmp / "g.xci"
        src.write_bytes(b"x" * 100)
        with small_split(), self.assertRaisesRegex(SakError, "lower than 4GB"):
            split.split_xci(src, self.tmp, Job())

    def test_trim_skips_padding_after_first_part(self):
        pad = b"\xff" * 32768
        data = pad + os.urandom(32768 * 3) + os.urandom(32768) + pad * 3
        src = self.tmp / "t.xci"
        src.write_bytes(data)
        with small_split():
            parts = split.split_xci(src, self.tmp / "o", Job(), trim=True)
        self.assertEqual(parts[0].read_bytes(), data[:PART])  # first part untouched
        self.assertEqual(parts[1].read_bytes(), data[PART:PART + 32768])

    def test_xci_00_naming(self):
        for n, chunk in enumerate([b"a" * 10, b"b" * 10, b"c" * 5]):
            (self.tmp / f"G.xci.{n:02d}").write_bytes(chunk)
        out = split.merge_xci(self.tmp / "G.xci.00", self.tmp / "o", Job())
        self.assertEqual(out.name, "G_merged.xci")
        self.assertEqual(out.read_bytes(), b"a" * 10 + b"b" * 10 + b"c" * 5)

    def test_folder_00_naming(self):
        folder = self.tmp / "Game.xci"
        folder.mkdir()
        (folder / "00").write_bytes(b"1")
        (folder / "01").write_bytes(b"2")
        out = split.merge_xci(folder / "00", self.tmp, Job())
        self.assertEqual((out.name, out.read_bytes()), ("Game_merged.xci", b"12"))

    def test_nsp_roundtrip(self):
        data = os.urandom(PART * 11 + 5)
        src = self.tmp / "A B.nsp"
        src.write_bytes(data)
        with small_split():
            folder = split.split_nsp(src, self.tmp / "o", Job())
        self.assertEqual(folder.name, "A_B_splitted.nsp")
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names[:3], ["00", "01", "02"])
        self.assertEqual(names[-1], "11")
        merged = split.merge_nsp(folder, self.tmp / "m", Job())
        self.assertEqual(merged.name, "A_B_merged.nsp")  # SAK strips "splitted_"
        self.assertEqual(merged.read_bytes(), data)

    def test_cancel_removes_parts(self):
        src = self.tmp / "c.xci"
        src.write_bytes(os.urandom(PART * 3))
        job = Job()
        job.on_progress = lambda pct: pct > 40 and job.cancel_event.set()
        with small_split(), self.assertRaises(SakError):
            split.split_xci(src, self.tmp / "o", job)
        self.assertEqual(list((self.tmp / "o").iterdir()), [])


class NacpTests(unittest.TestCase):
    def test_sak_defaults(self):
        raw = nacp.build_nacp(nacp.NacpConfig(name="My App", publisher="Me", display_version="2.1", screenshot=True))
        self.assertEqual(len(raw), 0x4000)
        info = nacp.parse_nacp(raw)
        self.assertEqual(len(info["titles"]), 12)
        self.assertEqual(info["titles"]["Portuguese"], ("My App", "Me"))
        self.assertNotIn("Portuguese", info["supported_languages"])
        self.assertEqual(len(info["supported_languages"]), 11)
        self.assertEqual(info["screenshot"], 0)  # Allow
        self.assertEqual(info["video_capture"], 0)  # Disable
        self.assertEqual(info["display_version"], "2.1")
        self.assertEqual(info["ratings"]["ESRB"], 10)
        self.assertEqual(info["ratings"]["ACB"], 13)
        self.assertNotIn("ClassInd", info["ratings"])
        self.assertEqual(info["presence_group_id"], 0x0104444444441001)
        self.assertEqual(info["logo_type"], 2)
        self.assertEqual(raw[0x3036], 1)  # DataLossConfirmation Required

    def test_long_name_truncated_utf8_safe(self):
        raw = nacp.build_nacp(nacp.NacpConfig(name="é" * 400))
        name = nacp.parse_nacp(raw)["titles"]["AmericanEnglish"][0]
        self.assertLess(len(name.encode()), 0x200)
        self.assertNotIn("�", name)


class ForwarderTests(Tmp):
    def spec(self, **kw):
        icon = self.tmp / "icon.jpg"
        icon.write_bytes(fake_jpeg())
        base = dict(name="Test", author="Me", nro_path="/switch/x/x.nro", icon=icon, title_id="0100ABCDEF012000")
        base.update(kw)
        return forwarder.ForwarderSpec(**base)

    def test_title_id_format(self):
        tid = forwarder.generate_title_id()
        self.assertRegex(tid, r"^01[0-9A-F]{11}000$")

    def test_stage_layout(self):
        root = self.tmp / "s"
        forwarder.stage(self.spec(), root)
        for rel in ["control/control.nacp", "control/icon_AmericanEnglish.dat", "exefs/main", "exefs/main.npdm",
                    "logo/NintendoLogo.png", "logo/StartupMovie.gif"]:
            self.assertTrue((root / rel).is_file(), rel)
        self.assertEqual((root / "romfs/nextArgv").read_text(), "sdmc:/switch/x/x.nro")
        self.assertEqual((root / "romfs/nextNroPath").read_text(), "sdmc:/switch/x/x.nro")

    def test_retroarch_argv(self):
        root = self.tmp / "s"
        forwarder.stage(self.spec(nro_path="/retroarch/cores/snes.nro", rom_path="/roms/a b.sfc"), root)
        self.assertEqual((root / "romfs/nextArgv").read_text(), 'sdmc:/retroarch/cores/snes.nro "sdmc:/roms/a b.sfc"')

    def test_validation(self):
        with self.assertRaisesRegex(SakError, "16 chars"):
            forwarder.validate(self.spec(title_id="123"))
        bad = self.tmp / "bad.jpg"
        bad.write_bytes(fake_jpeg(128, 128))
        with self.assertRaisesRegex(SakError, "256x256"):
            forwarder.validate(self.spec(icon=bad))

    def test_build_with_fake_hacbrewpack(self):
        argv_log = self.tmp / "argv.json"
        fake = write_exe(self.tmp / "hacbrewpack", f"""
import json, sys, pathlib
a = sys.argv[1:]
json.dump(a, open({str(argv_log)!r}, "w"))
d = pathlib.Path(a[a.index("--nspdir") + 1]); d.mkdir(parents=True, exist_ok=True)
(d / (a[a.index("--titleid") + 1].lower() + ".nsp")).write_bytes(b"PFS0")
""")
        keys = self.tmp / "prod.keys"
        keys.write_text("x = 00\n")
        with mock.patch.dict(os.environ, {"SAK_HACBREWPACK": str(fake)}):
            out = forwarder.build(self.spec(no_logo=True), self.tmp / "out", Job(), keys)
        self.assertEqual(out.name, "Test-0100abcdef012000.nsp")
        argv = json.loads(argv_log.read_text())
        self.assertIn("--nologo", argv)
        self.assertEqual(argv[argv.index("--titlename") + 1], "Test")
        self.assertEqual([p.name for p in (self.tmp / "out").iterdir()], [out.name])  # temp tree cleaned


class OpsTests(Tmp):
    def fake_squirrel(self, ext):
        log = self.tmp / "sq.json"
        script = self.tmp / "squirrel.py"
        script.write_text(f"""
import json, os, sys, pathlib
a = sys.argv[1:]
json.dump({{"argv": a, "cwd": os.getcwd(), "keys": os.environ.get("NSCB_KEYS_FILE"),
           "list": open(a[a.index("-tfile") + 1]).read() if "-tfile" in a else None}}, open({str(log)!r}, "w"))
o = pathlib.Path(a[a.index("-o") + 1]); (o / "Game [0100000000010000].{ext}").write_bytes(b"x")
""")
        return script, log

    def test_compress_args_and_output(self):
        script, log = self.fake_squirrel("nsz")
        src = self.tmp / "g.nsp"
        src.write_bytes(b"n")
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(script)}):
            out = ops.compress(src, self.tmp / "out", Job(), "hard", keys)
        rec = json.loads(log.read_text())
        self.assertEqual(rec["keys"], str(keys.resolve()))
        self.assertIn("-W", tools.squirrel_cmd())
        self.assertEqual(rec["argv"][rec["argv"].index("--compress") + 1], "22")
        self.assertEqual(rec["list"], f"{src.resolve()}\n")
        self.assertEqual([p.name for p in out], ["Game [0100000000010000].nsz"])
        self.assertFalse(any(p.name.startswith("sak-squirrel") for p in (self.tmp / "out").iterdir()))

    def test_uppercase_extension_is_lowercased_for_squirrel(self):
        script, log = self.fake_squirrel("nsz")
        src = self.tmp / "G.NSP"
        src.write_bytes(b"n")
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(script)}):
            ops.compress(src, self.tmp / "out", Job(), 1, keys)
        listed = json.loads(log.read_text())["list"].strip()
        self.assertTrue(listed.endswith("G.nsp"))

    def test_missing_input_rejected_before_squirrel(self):
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(self.tmp / "nope.py")}), self.assertRaises(SakError):
            ops.update_title(self.tmp / "missing.xci", [], self.tmp, Job(), True, keys)

    def test_truncated_output_rejected(self):
        script, _ = self.fake_squirrel("nsp")  # writes a 1-byte "result"
        src = self.tmp / "big.nsp"
        with src.open("wb") as fh:
            fh.truncate(200 * 1024 * 1024)  # sparse
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(script)}), self.assertRaisesRegex(SakError, "truncated"):
            ops.update_title(src, [], self.tmp / "out", Job(), False, keys)

    def test_patch_renames(self):
        script, log = self.fake_squirrel("xci")
        src = self.tmp / "g.xci"
        src.write_bytes(b"n")
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(script)}):
            out = ops.patch_xci(src, self.tmp / "out", Job(), keys)
        argv = json.loads(log.read_text())["argv"]
        self.assertEqual(argv[argv.index("-kp") + 1], "0")
        self.assertEqual(out[0].name, "Game [0100000000010000]_patched.xci")

    def test_no_output_is_error(self):
        script = self.tmp / "squirrel.py"
        script.write_text("print('nothing')\n")
        src = self.tmp / "g.nsp"
        src.write_bytes(b"n")
        keys = self.tmp / "prod.keys"
        keys.write_text("k = 1\n")
        with mock.patch.dict(os.environ, {"SAK_SQUIRREL": str(script)}), self.assertRaisesRegex(SakError, "keys"):
            ops.convert_nsp_to_xci(src, self.tmp, Job(), keys)

    def test_firmware_mapping(self):
        csv = self.tmp / "m.csv"
        csv.write_text("1.0.0;117f7b9c7da3e8cef02340596af206b3\r\n2.0.0;7a1f79f8184d4b9bae1755090278f52c\r\n")
        self.assertEqual(ops.firmware_version(["x.nca", "7a1f79f8184d4b9bae1755090278f52c.nca"], csv), "2.0.0")
        self.assertEqual(ops.firmware_version(["x.nca"], csv), "Unknown")

    def test_bundled_firmware_mapping_parses(self):
        self.assertEqual(ops.firmware_version(["117f7b9c7da3e8cef02340596af206b3.nca"]), "1.0.0")

    def test_save_meta_parsing(self):
        out = "Title ID:   0100000000010000\nJSON: " + json.dumps(
            {"title_id": "0100000000010000", "user_id": "ab", "save_id": "0000000000000001",
             "save_type": "Account", "timestamp": "2021-01-02 03:04:05"})
        self.assertEqual(ops._parse_save_meta(out)["save_type"], "Account")
        legacy = "Title ID: 0100\nUser ID: u\nSave ID: s\nSave Type: Account\n"
        self.assertEqual(ops._parse_save_meta(legacy)["user_id"], "u")


def build_pfs0(files: dict[str, bytes]) -> bytes:
    names = b"".join(n.encode() + b"\0" for n in files)
    names += b"\0" * ((-len(names)) % 0x20)
    table, data, name_off = b"", b"", 0
    for n, blob in files.items():
        table += struct.pack("<QQII", len(data), len(blob), name_off, 0)
        name_off += len(n) + 1
        data += blob
    return b"PFS0" + struct.pack("<III", len(files), len(names), 0) + table + names + data


def build_cnmt(title_id: int, version: int, meta_type: int, contents: list[tuple[str, int]]) -> bytes:
    head = struct.pack("<QIBB", title_id, version, meta_type, 0) + struct.pack("<HHH", 0x10, len(contents), 0)
    head = head.ljust(0x20, b"\0") + b"\0" * 0x10  # 0x10-byte extended header
    for nca_id, ctype in contents:
        head += b"\0" * 0x20 + bytes.fromhex(nca_id) + (0).to_bytes(6, "little") + bytes([ctype, 0])
    return head


PROG_ID = "aa" * 16
META_ID = "bb" * 16


class ExtractTests(Tmp):
    def fake_hactool(self, title_id=0x01006BB00C6F0000, version=0, meta_type=0x80,
                     build_id="ae16f71e002af8cb" + "00" * 24, with_sdk=True):
        cnmt = self.tmp / "fake.cnmt"
        cnmt.write_bytes(build_cnmt(title_id, version, meta_type, [(PROG_ID, 1), (META_ID, 0)]))
        log = self.tmp / "hactool.json"
        exe = write_exe(self.tmp / "hactool", f"""
import json, os, pathlib, shutil, sys
a = sys.argv[1:]
with open({str(log)!r}, "a") as fh: fh.write(json.dumps(a) + "\\n")
print('[WARN]: Failed to match key "secret_key", (value "deadbeef")')
opt = {{x.split("=", 1)[0]: x.split("=", 1)[1] for x in a if x.startswith("--") and "=" in x}}
if "--section0dir" in opt:
    d = pathlib.Path(opt["--section0dir"]); d.mkdir()
    shutil.copy({str(cnmt)!r}, d / "Application_x.cnmt")
if "--exefsdir" in opt:
    e = pathlib.Path(opt["--exefsdir"]); e.mkdir()
    (e / "main").write_bytes(b"NSO0" + b"\\0" * 0x3C + bytes.fromhex({build_id!r}))
    if {with_sdk!r}: (e / "sdk").write_bytes(b"NSO0")
    r = pathlib.Path(opt["--romfsdir"]); r.mkdir(); (r / "a.bin").write_bytes(b"x")
""")
        return exe, log

    def nsp(self, extra=None):
        files = {f"{META_ID}.cnmt.nca": b"meta", f"{PROG_ID}.nca": b"program" * 10}
        files.update(extra or {})
        p = self.tmp / "game.nsp"
        p.write_bytes(build_pfs0(files))
        return p

    def keys(self):
        k = self.tmp / "prod.keys"
        k.write_text("good_key = " + "ab" * 16 + "\nbad_kek_source_06 = " + "c" * 34 + "\n")
        return k

    def test_read_container_pfs0(self):
        entries = extract.read_container(self.nsp())
        self.assertEqual(sorted(entries), sorted([f"{META_ID}.cnmt.nca", f"{PROG_ID}.nca"]))
        e = entries[f"{PROG_ID}.nca"]
        with open(self.tmp / "game.nsp", "rb") as fh:
            fh.seek(e.offset)
            self.assertEqual(fh.read(e.size), b"program" * 10)

    def test_parse_cnmt(self):
        meta = extract.parse_cnmt(build_cnmt(0x0100ABCD00010000, 65536, 0x81, [(PROG_ID, 1), (META_ID, 0)]))
        self.assertEqual((meta.title_id, meta.version, meta.type_name), ("0100ABCD00010000", 65536, "Patch"))
        self.assertEqual(meta.programs, [(PROG_ID, 0)])

    def test_ticket_titlekey(self):
        tik = bytearray(0x2C0)
        struct.pack_into("<I", tik, 0, 0x10004)
        tik[0x180:0x190] = bytes(range(16))
        tik[0x2A0:0x2B0] = bytes.fromhex("01006bb00c6f0000000000000000000a")
        self.assertEqual(extract.ticket_titlekey(bytes(tik)), ("01006bb00c6f0000000000000000000a", bytes(range(16)).hex()))
        tik[0x281] = 1  # personalized
        with self.assertRaisesRegex(SakError, "personalized"):
            extract.ticket_titlekey(bytes(tik))

    def test_sanitized_keys_drop_malformed_and_cleanup(self):
        with tools.sanitized_keys(self.keys()) as clean:
            text = clean.read_text()
            self.assertEqual(oct(clean.stat().st_mode & 0o777), "0o600")
        self.assertIn("good_key", text)
        self.assertNotIn("bad_kek_source", text)
        self.assertFalse(clean.exists())

    def test_lasrecomp_flow(self):
        exe, log = self.fake_hactool()
        keys = self.keys()
        out = self.tmp / "out"
        lines = []
        job = Job(on_log=lines.append)
        with mock.patch.dict(os.environ, {"SAK_HACTOOL": str(exe)}):
            res = extract.extract_title(self.nsp(), extract.default_dest(self.tmp / "game.nsp", out, True), job, keys, True)
        self.assertEqual(res.folder, (out / "extracted").resolve())
        self.assertEqual(sorted(p.name for p in res.folder.iterdir()), ["exefs", "romfs"])
        self.assertEqual(res.exefs_files, ["main", "sdk"])
        self.assertTrue(res.build_id.startswith("ae16f71e002af8cb"))
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        keyfile = calls[0][calls[0].index("-k") + 1]
        self.assertNotEqual(keyfile, str(keys))  # sanitized copy, not the user's file
        self.assertFalse(Path(keyfile).exists())
        self.assertFalse(any("Failed to match key" in l for l in lines))  # key echo redacted
        self.assertEqual([p.name for p in out.iterdir()], ["extracted"])  # temp dirs cleaned

    def test_lasrecomp_rejects_update_and_wrong_build(self):
        for kw, msg in [(dict(version=65536), "v1.0.0"), (dict(title_id=0x0100000000010000), "Link's Awakening"),
                        (dict(build_id="11" * 32), "build id"), (dict(with_sdk=False), "sdk")]:
            exe, _ = self.fake_hactool(**kw)
            with mock.patch.dict(os.environ, {"SAK_HACTOOL": str(exe)}), self.assertRaisesRegex(SakError, msg):
                extract.extract_title(self.nsp(), self.tmp / f"o{len(msg)}" / "extracted", Job(), self.keys(), True)

    def test_generic_refuses_patch_and_uses_ticket(self):
        exe, _ = self.fake_hactool(meta_type=0x81)
        with mock.patch.dict(os.environ, {"SAK_HACTOOL": str(exe)}), self.assertRaisesRegex(SakError, "Patch"):
            extract.extract_title(self.nsp(), self.tmp / "p", Job(), self.keys())
        tik = bytearray(0x2C0)
        struct.pack_into("<I", tik, 0, 0x10004)
        tik[0x180:0x190] = b"\x11" * 16
        exe, log = self.fake_hactool(title_id=0x0100000000010000)
        nsp = self.nsp({"0100000000010000000000000000000a.tik": bytes(tik)})
        with mock.patch.dict(os.environ, {"SAK_HACTOOL": str(exe)}):
            extract.extract_title(nsp, self.tmp / "g", Job(), self.keys())
        extract_call = json.loads(log.read_text().splitlines()[-1])
        self.assertIn("--titlekey=" + "11" * 16, extract_call)


class GuiTests(unittest.TestCase):
    def test_token_and_host_enforced(self):
        from http.server import ThreadingHTTPServer

        from sak.gui import server

        app = server.App(None, tempfile.gettempdir())
        port = [0]
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, port))
        port[0] = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def req(path, headers):
                c = http.client.HTTPConnection("127.0.0.1", port[0])
                c.request("GET", path, headers=headers)
                r = c.getresponse()
                try:
                    return r.status, r.read()
                finally:
                    c.close()

            host = {"Host": f"127.0.0.1:{port[0]}"}
            self.assertEqual(req("/api/state", host)[0], 403)
            self.assertEqual(req("/api/state", {"Host": "evil.example", "X-SAK-Token": app.token})[0], 403)
            status, body = req("/api/state", {**host, "X-SAK-Token": app.token})
            self.assertEqual(status, 200)
            self.assertIn("tools", json.loads(body))
            status, body = req("/", host)
            self.assertEqual(status, 200)
            self.assertIn(app.token.encode(), body)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
