"""Shared helpers: progress/cancel plumbing, free-space checks, name rules."""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# SAK copies in 32 KiB blocks and splits at 0xFFFF0000 bytes (just under 4 GiB, FAT32 limit).
READ_BYTES = 32768
SPLIT_BYTES = 4294901760
# SAK requires the payload size plus 100 MB of free space before it starts.
FREE_SPACE_MARGIN = 100 * 1024 * 1024


class SakError(Exception):
    """User-facing failure (bad input, missing tool, not enough space...)."""


class Cancelled(SakError):
    pass


@dataclass
class Job:
    """Progress sink + cancel flag shared between an operation and its UI."""

    on_progress: Callable[[int], None] | None = None
    on_log: Callable[[str], None] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _last_pct: int = -1

    def progress(self, pct: float) -> None:
        pct = max(0, min(100, int(round(pct))))
        if pct != self._last_pct:
            self._last_pct = pct
            if self.on_progress:
                self.on_progress(pct)

    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled("cancelled by user")


def human_size(num_bytes: float) -> str:
    """SAK's size formatting: MB below 1000 MB, otherwise GB, 2 decimals."""
    mb = round(num_bytes / 1024 / 1024, 2)
    if mb >= 1000:
        return f"{round(mb / 1024, 2)}GB"
    return f"{mb}MB"


def ensure_free_space(target_dir: Path, needed_bytes: int, what: str) -> None:
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < needed_bytes + FREE_SPACE_MARGIN:
        raise SakError(
            f"{what} aborted\nFree space is to low\nOnly {round(free / 1024**3, 2)}GB free\n"
            f"Needed min size: {human_size(needed_bytes + FREE_SPACE_MARGIN)}"
        )


def ireplace(text: str, old: str, new: str) -> str:
    """AutoIt StringReplace is case-insensitive by default."""
    return re.sub(re.escape(old), lambda _m: new, text, flags=re.IGNORECASE)


def copy_stream(src, dst, total: int, done: int, job: Job, skip_chunk=None) -> int:
    """Copy src to dst in READ_BYTES blocks; returns updated byte counter."""
    while True:
        job.check()
        chunk = src.read(READ_BYTES)
        if not chunk:
            return done
        if skip_chunk is None or not skip_chunk(chunk):
            dst.write(chunk)
        done += len(chunk)
        job.progress(done * 100 / total if total else 100)
