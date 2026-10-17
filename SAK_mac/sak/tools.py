"""Locating external tools/keys and running them (SAK's `cmd /c ... && tool` replacement)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .common import Cancelled, Job, SakError

PORT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = Path(__file__).resolve().parent / "resources"
TOOLS_BIN = PORT_ROOT / "tools" / "bin"
NSCB_ZTOOLS = PORT_ROOT / "tools" / "nscb" / "py" / "ztools"
VENV_PYTHON = PORT_ROOT / ".venv" / "bin" / "python"

KEY_SEARCH = [
    PORT_ROOT / "keys" / "prod.keys",
    PORT_ROOT / "keys" / "keys.dat",
    Path.home() / ".switch" / "prod.keys",
]


def find_keys(explicit: str | os.PathLike | None = None) -> Path:
    """SAK used bin/prod.keys if present, else bin/keys.dat."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("SAK_KEYS"):
        candidates.append(Path(os.environ["SAK_KEYS"]).expanduser())
    candidates += KEY_SEARCH
    for c in candidates:
        if c.is_file():
            return c
    raise SakError(
        "keys.dat/prod.keys missing\nPlace your prod.keys in SAK_mac/keys/ or ~/.switch/, "
        "set SAK_KEYS, or pass --keys to use this feature!"
    )


def find_tool(name: str) -> Path:
    env = os.environ.get(f"SAK_{name.upper()}")
    for c in [Path(env) if env else None, TOOLS_BIN / name]:
        if c and c.is_file() and os.access(c, os.X_OK):
            return c
    found = shutil.which(name)
    if found:
        return Path(found)
    raise SakError(f"{name} not found - run scripts/setup.sh (or set SAK_{name.upper()})")


def squirrel_cmd() -> list[str]:
    script = Path(os.environ.get("SAK_SQUIRREL", NSCB_ZTOOLS / "squirrel.py"))
    if not script.is_file():
        raise SakError("NSC_Builder squirrel.py not found - run scripts/setup.sh")
    python = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    return [str(python), "-W", "ignore::SyntaxWarning", str(script)]


def resource(name: str) -> Path:
    path = RESOURCES / name
    if not path.is_file():
        raise SakError(f"missing resource {name}")
    return path


def run(cmd: list[str], job: Job, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    """Run a tool, streaming merged stdout/stderr to the job log; kill it on cancel."""
    job.log("$ " + " ".join(_quote(c) for c in cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    output: list[str] = []

    def pump() -> None:
        assert proc.stdout
        buf = b""
        while True:
            data = proc.stdout.read1(4096)
            if not data:
                break
            buf += data
            # tools redraw progress bars with \r; treat it as a line break
            *lines, buf = buf.replace(b"\r", b"\n").split(b"\n")
            for line in lines:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    output.append(text)
                    job.log(text)
        if buf.strip():
            output.append(buf.decode("utf-8", "replace").rstrip())
            job.log(output[-1])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    while proc.poll() is None:
        if job.cancelled:
            proc.kill()
            proc.wait()
            reader.join(timeout=5)
            raise Cancelled("cancelled by user")
        reader.join(timeout=0.2)
    reader.join()
    return proc.returncode, "\n".join(output)


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg
