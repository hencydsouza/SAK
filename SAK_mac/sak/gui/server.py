"""Local web GUI (stdlib only). Binds 127.0.0.1, every API call needs the per-launch token."""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__, drives, extract, forwarder, ops, split, tools
from ..common import Cancelled, Job, SakError

INDEX = Path(__file__).with_name("index.html")


class JobState:
    def __init__(self, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.status = "running"
        self.progress = 0
        self.log: list[str] = []
        self.result: list[str] = []
        self.error = ""
        self.job = Job(on_progress=self._progress, on_log=self.log.append)

    def _progress(self, pct: int) -> None:
        self.progress = pct


def _paths(v) -> list[Path]:
    if isinstance(v, str):
        v = [v]
    return [Path(p).expanduser() for p in v or [] if str(p).strip()]


def _one(v) -> Path:
    paths = _paths(v)
    if not paths:
        raise SakError("no input selected")
    return paths[0]


def _each(files, fn):
    out = []
    for f in _paths(files):
        r = fn(f)
        out.extend(r if isinstance(r, list) else [r])
    if not out and not _paths(files):
        raise SakError("no input selected")
    return out


def _keys(p):
    return tools.find_keys(p.get("keys") or None)


OPS = {
    "split_xci": ("Split XCI", lambda p, o, j: _each(p["files"], lambda f: split.split_xci(f, o, j, trim=bool(p.get("trim"))))),
    "merge_xci": ("Merge XCI", lambda p, o, j: _each(p["files"], lambda f: split.merge_xci(f, o, j))),
    "split_nsp": ("Split NSP", lambda p, o, j: _each(p["files"], lambda f: split.split_nsp(f, o, j))),
    "merge_nsp": ("Merge NSP", lambda p, o, j: _each(p["files"], lambda f: split.merge_nsp(f, o, j))),
    "xci2nsp": ("XCI to NSP", lambda p, o, j: _each(p["files"], lambda f: ops.convert_xci_to_nsp(f, o, j, _keys(p), bool(p.get("split"))))),
    "nsp2xci": ("NSP to XCI", lambda p, o, j: _each(p["files"], lambda f: ops.convert_nsp_to_xci(f, o, j, _keys(p)))),
    "patch_xci": ("Patch XCI", lambda p, o, j: _each(p["files"], lambda f: ops.patch_xci(f, o, j, _keys(p)))),
    "patch_nsp": ("Patch NSP", lambda p, o, j: _each(p["files"], lambda f: ops.patch_nsp(f, o, j, _keys(p), bool(p.get("split"))))),
    "update_xci": ("Update XCI", lambda p, o, j: ops.update_title(_one(p["base"]), _paths(p["files"]), o, j, True, _keys(p))),
    "update_nsp": ("Update NSP", lambda p, o, j: ops.update_title(_one(p["base"]), _paths(p["files"]), o, j, False, _keys(p))),
    "compress": ("Compress", lambda p, o, j: _each(p["files"], lambda f: ops.compress(f, o, j, p.get("level", "normal"), _keys(p)))),
    "decompress": ("Decompress", lambda p, o, j: _each(p["files"], lambda f: ops.decompress(f, o, j, _keys(p)))),
    "extract_fw": ("Extract firmware", lambda p, o, j: _each(p["files"], lambda f: ops.extract_firmware(f, o, j, _keys(p)))),
    "extract_saves": ("Extract saves", lambda p, o, j: [ops.extract_saves(_one(p["save_dir"]), o, j, _keys(p))]),
    "extract": ("Extract exefs/romfs", lambda p, o, j: _each(p["files"], lambda f: str(extract.extract_title(
        f, extract.default_dest(f, o, p.get("preset") or None,
                                _paths(p.get("lasrecomp_dir"))[0] if _paths(p.get("lasrecomp_dir")) else None),
        j, _keys(p), p.get("preset") or None, bool(p.get("exefs_only"))).folder))),
    "forwarder": ("NRO forwarder", lambda p, o, j: [forwarder.build(forwarder.ForwarderSpec(
        name=p.get("name", ""), author=p.get("author", "AUTHOR"), nro_path=p.get("nro", ""),
        icon=_one(p.get("icon")), rom_path=p.get("rom") or None, title_id=p.get("title_id", ""),
        version=p.get("version", "1.0"), screenshot=bool(p.get("screenshot")),
        video_capture=bool(p.get("video_capture")), no_logo=bool(p.get("no_logo")),
        custom_logo_dir=_paths(p.get("logo_dir"))[0] if _paths(p.get("logo_dir")) else None,
    ), o, j, _keys(p))]),
    "fat32": ("Format FAT32", lambda p, o, j: drives.format_fat32(
        p.get("disk", ""), p.get("label", "SWITCH"), j, p.get("confirm", "")) or []),
}


def _osascript_pick(kind: str, prompt: str, types: list[str]) -> list[str]:
    prompt = prompt.replace('"', "'")
    if kind == "folder":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
    else:
        of_type = ""
        if types:
            of_type = " of type {" + ", ".join(f'"{t}"' for t in types if t.isalnum()) + "}"
        multi = " with multiple selections allowed" if kind == "files" else ""
        script = (
            f'set L to (choose file with prompt "{prompt}"{of_type}{multi})\n'
            'if class of L is not list then set L to {L}\n'
            'set out to ""\nrepeat with f in L\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out'
        )
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        return []  # user cancelled
    return [line for line in res.stdout.splitlines() if line.strip()]


def _windows_pick(kind: str, prompt: str, types: list[str]) -> list[str]:
    """Native Open/Folder dialogs through PowerShell + WinForms (no extra Python packages)."""
    title = prompt.replace("'", "''")
    owner = "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false}"
    if kind == "folder":
        body = (f"$d = New-Object System.Windows.Forms.FolderBrowserDialog -Property @{{Description='{title}'; "
                "ShowNewFolderButton=$true}\nif ($d.ShowDialog($o) -eq 'OK') { $d.SelectedPath }")
    else:
        exts = [t for t in types if t.isalnum()]
        pattern = ";".join(f"*.{t}" for t in exts)
        flt = (f"{'/'.join(e.upper() for e in exts)} files|{pattern}|" if exts else "") + "All files|*.*"
        multi = "$true" if kind == "files" else "$false"
        body = (f"$d = New-Object System.Windows.Forms.OpenFileDialog -Property @{{Title='{title}'; "
                f"Filter='{flt}'; Multiselect={multi}}}\nif ($d.ShowDialog($o) -eq 'OK') {{ $d.FileNames }}")
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\nAdd-Type -AssemblyName System.Windows.Forms\n"
              f"{owner}\n{body}")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def pick(kind: str, prompt: str, types: list[str]) -> list[str]:
    return (_windows_pick if sys.platform == "win32" else _osascript_pick)(kind, prompt, types)


class App:
    def __init__(self, keys: str | None, out: str):
        self.token = secrets.token_urlsafe(24)
        self.keys = keys or ""
        self.out = str(Path(out).expanduser().resolve())
        self.jobs: dict[str, JobState] = {}
        self.lock = threading.Lock()

    def state(self) -> dict:
        status = {}
        for name in tools.NATIVE_TOOLS:
            try:
                status[name] = str(tools.find_tool(name))
            except SakError:
                status[name] = None
        try:
            status["squirrel"] = " ".join(tools.squirrel_cmd())
        except SakError:
            status["squirrel"] = None
        try:
            keys = str(tools.find_keys(self.keys or None))
        except SakError:
            keys = None
        return {"version": __version__, "platform": "windows" if sys.platform == "win32" else "macos", "tools": status, "keys": keys, "keys_setting": self.keys,
                "out": self.out, "default_title_id": forwarder.DEFAULT_TITLE_ID}

    def start(self, op: str, params: dict) -> JobState:
        if op not in OPS:
            raise SakError(f"unknown operation {op}")
        title, fn = OPS[op]
        if any(j.status == "running" for j in self.jobs.values()):
            raise SakError("another job is still running")
        state = JobState(title)
        params = {**params, "keys": params.get("keys") or self.keys}
        out = Path(params.get("out") or self.out).expanduser()

        def work():
            try:
                out.mkdir(parents=True, exist_ok=True)
                state.result = [str(r) for r in fn(params, out, state.job)]
                state.status = "done"
                state.progress = 100
            except Cancelled:
                state.status = "cancelled"
            except SakError as e:
                state.status, state.error = "error", str(e)
            except Exception as e:  # surface unexpected failures in the UI
                state.status, state.error = "error", f"{e}\n{traceback.format_exc()}"

        with self.lock:
            self.jobs[state.id] = state
        threading.Thread(target=work, daemon=True).start()
        return state


def make_handler(app: App, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            # blocks DNS-rebinding: only our own loopback origin may talk to us
            return self.headers.get("Host") in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _authorized(self) -> bool:
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-SAK-Token", ""), app.token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if self.path in ("/", "/index.html"):
                html = INDEX.read_text(encoding="utf-8").replace("__SAK_TOKEN__", app.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            if self.path == "/api/state":
                return self._send(200, app.state())
            if self.path == "/api/drives":
                try:
                    return self._send(200, [asdict(d) for d in drives.external_disks()])
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if self.path == "/api/title-id":
                return self._send(200, {"title_id": forwarder.generate_title_id()})
            if self.path.startswith("/api/job/"):
                job = app.jobs.get(self.path.rsplit("/", 1)[-1])
                if not job:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, {"id": job.id, "title": job.title, "status": job.status,
                                        "progress": job.progress, "log": job.log[-400:],
                                        "result": job.result, "error": job.error})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            try:
                if self.path == "/api/run":
                    job = app.start(body.get("op", ""), body.get("params", {}))
                    return self._send(200, {"id": job.id})
                if self.path.startswith("/api/cancel/"):
                    job = app.jobs.get(self.path.rsplit("/", 1)[-1])
                    if job:
                        job.job.cancel_event.set()
                    return self._send(200, {"ok": bool(job)})
                if self.path == "/api/pick":
                    return self._send(200, {"paths": pick(
                        body.get("kind", "file"), body.get("prompt", "Select"), body.get("types", []))})
                if self.path == "/api/settings":
                    if "keys" in body:
                        app.keys = str(body["keys"] or "")
                    if body.get("out"):
                        app.out = str(Path(body["out"]).expanduser())
                    return self._send(200, app.state())
            except SakError as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = 0, open_browser: bool = True, keys: str | None = None, out: str = ".") -> None:
    app = App(keys, out)
    port_ref = [0]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref[0] = httpd.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/"
    print(f"SAK GUI running at {url}  (Ctrl+C to quit)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for j in app.jobs.values():
            j.job.cancel_event.set()
        httpd.server_close()
