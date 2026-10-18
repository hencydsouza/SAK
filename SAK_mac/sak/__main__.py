import sys

from .cli import main

if sys.stdout is None or sys.stderr is None:
    # pythonw.exe (the Windows GUI launcher) has no console: keep a log beside the app instead.
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    stream = open(log_dir / "sak.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream

sys.exit(main())
