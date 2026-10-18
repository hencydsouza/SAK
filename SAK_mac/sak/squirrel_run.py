"""Run NSC_Builder's squirrel.py as a script: `python -m sak.squirrel_run <squirrel.py> args...`.

The Windows build uses an embeddable Python whose ._pth file switches on isolated mode, so
the script's own folder (which holds squirrel's Fs/nutFs packages) is not put on sys.path
and PYTHONPATH is ignored. This restores normal script semantics.
"""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m sak.squirrel_run <squirrel.py> [args...]")
    script = Path(sys.argv[1]).resolve()
    sys.argv = [str(script)] + sys.argv[2:]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
