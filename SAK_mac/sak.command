#!/usr/bin/env bash
# Double-click in Finder to open the SAK GUI; from a terminal use ./sak.command <command> ...
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
cd "$ROOT"
if [[ $# -eq 0 ]]; then
  exec "$PY" -m sak -o "$HOME/SAK" gui
fi
exec "$PY" -m sak "$@"
